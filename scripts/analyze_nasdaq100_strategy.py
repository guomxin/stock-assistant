#!/usr/bin/env python3
"""Analyze Nasdaq-100 with a score model and systematic allocation backtest."""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

import numpy as np
import pandas as pd


BASE_DIR = Path(__file__).resolve().parents[1]
RAW_INDEX_DIR = BASE_DIR / "data" / "raw" / "index_daily"
ANALYSIS_DIR = BASE_DIR / "analysis" / "nasdaq100"

START_DATE = "20050101"
DEFAULT_COST = 0.0005
TARGET = ("^NDX", "NDX.YAHOO", "纳斯达克100")
BENCHMARKS = [
    ("^GSPC", "GSPC.YAHOO", "标普500"),
    ("^IXIC", "IXIC.YAHOO", "纳斯达克综合"),
]


@dataclass(frozen=True)
class ComponentSpec:
    key: str
    label: str
    weight: float
    value_label: str


@dataclass(frozen=True)
class Metric:
    cagr: float
    total_return: float
    max_drawdown: float
    annual_vol: float
    sharpe: float
    exposure: float
    turnover: float
    changes: int
    years: float


COMPONENTS = [
    ComponentSpec("price_pct_3y", "三年价格位置", 0.16, "近3年价格分位"),
    ComponentSpec("dd_3y", "三年回撤位置", 0.12, "距3年高点回撤"),
    ComponentSpec("ma200_dev", "年线偏离", 0.12, "相对200日均线"),
    ComponentSpec("rsi14", "短期拥挤度", 0.08, "RSI14"),
    ComponentSpec("ret_60", "三个月涨幅", 0.10, "60日涨幅"),
    ComponentSpec("ret_120", "半年涨幅", 0.10, "120日涨幅"),
    ComponentSpec("ret_250", "一年涨幅", 0.12, "250日涨幅"),
    ComponentSpec("vol_60", "波动风险", 0.08, "60日年化波动"),
    ComponentSpec("rel_250_GSPC_YAHOO", "相对标普500热度", 0.08, "一年相对标普500"),
    ComponentSpec("rel_250_IXIC_YAHOO", "相对纳综热度", 0.04, "一年相对纳斯达克综合"),
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build Nasdaq-100 score and strategy research report.")
    parser.add_argument("--start-date", default=START_DATE)
    parser.add_argument("--end-date", default=datetime.now().strftime("%Y%m%d"))
    parser.add_argument("--refresh", action="store_true", help="Refetch Yahoo data even if cache exists.")
    parser.add_argument("--cost", type=float, default=DEFAULT_COST, help="Cost charged on absolute position changes.")
    return parser.parse_args()


def parse_yyyymmdd(value: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y%m%d").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise SystemExit(f"Invalid date {value!r}; expected YYYYMMDD") from exc


def safe_code(code: str) -> str:
    return code.replace("^", "").replace(".", "_").replace("/", "_")


def yahoo_chart_url(symbol: str, start_date: str, end_date: str) -> str:
    start_dt = parse_yyyymmdd(start_date)
    end_dt = parse_yyyymmdd(end_date) + timedelta(days=1)
    period1 = int(start_dt.timestamp())
    period2 = int(end_dt.timestamp())
    return (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        f"{quote(symbol)}?period1={period1}&period2={period2}&interval=1d&events=history"
    )


def cache_path(output_code: str, start_date: str, end_date: str) -> Path:
    return RAW_INDEX_DIR / f"{safe_code(output_code)}_{start_date}_{end_date}.parquet"


def fetch_yahoo_daily(symbol: str, output_code: str, start_date: str, end_date: str, refresh: bool) -> pd.DataFrame:
    RAW_INDEX_DIR.mkdir(parents=True, exist_ok=True)
    path = cache_path(output_code, start_date, end_date)
    if path.exists() and not refresh:
        return pd.read_parquet(path)

    request = Request(yahoo_chart_url(symbol, start_date, end_date), headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))

    error = payload.get("chart", {}).get("error")
    if error:
        raise RuntimeError(f"Yahoo chart error for {symbol}: {error}")
    results = payload.get("chart", {}).get("result") or []
    if not results:
        raise RuntimeError(f"No Yahoo chart data returned for {symbol}")

    result = results[0]
    timestamps = result.get("timestamp") or []
    quote_data = (result.get("indicators", {}).get("quote") or [{}])[0]
    if not timestamps:
        raise RuntimeError(f"No daily timestamps returned for {symbol}")

    df = pd.DataFrame(
        {
            "ts_code": output_code,
            "trade_date": [datetime.fromtimestamp(ts, timezone.utc).strftime("%Y%m%d") for ts in timestamps],
            "open": quote_data.get("open", []),
            "close": quote_data.get("close", []),
            "high": quote_data.get("high", []),
            "low": quote_data.get("low", []),
            "vol": quote_data.get("volume", []),
        }
    )
    for column in ["open", "close", "high", "low", "vol"]:
        df[column] = pd.to_numeric(df[column], errors="coerce")

    df = df.dropna(subset=["close"]).sort_values("trade_date").reset_index(drop=True)
    df["pre_close"] = df["close"].shift(1)
    df["change"] = df["close"] - df["pre_close"]
    df["pct_chg"] = df["change"] / df["pre_close"] * 100
    df.loc[df["pre_close"].isna(), ["change", "pct_chg"]] = pd.NA
    df = df[["ts_code", "trade_date", "open", "close", "high", "low", "pre_close", "change", "pct_chg", "vol"]]
    df.to_parquet(path, index=False)
    return df


def rsi(series: pd.Series, window: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / window, adjust=False, min_periods=window).mean()
    avg_loss = loss.ewm(alpha=1 / window, adjust=False, min_periods=window).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def rolling_percentile_last(series: pd.Series, window: int) -> pd.Series:
    def calc(values: np.ndarray) -> float:
        current = values[-1]
        valid = values[np.isfinite(values)]
        if not np.isfinite(current) or len(valid) == 0:
            return np.nan
        return float((valid <= current).sum() / len(valid))

    return series.rolling(window, min_periods=max(60, window // 3)).apply(calc, raw=True)


def expanding_percentile_last(series: pd.Series, min_periods: int = 252) -> pd.Series:
    def calc(values: np.ndarray) -> float:
        current = values[-1]
        valid = values[np.isfinite(values)]
        if not np.isfinite(current) or len(valid) < min_periods:
            return np.nan
        return float((valid <= current).sum() / len(valid))

    return series.expanding(min_periods=min_periods).apply(calc, raw=True)


def build_indicator_frame(target: pd.DataFrame, benchmarks: dict[str, pd.DataFrame]) -> pd.DataFrame:
    df = target[["trade_date", "close", "pct_chg"]].copy()
    df["trade_date"] = df["trade_date"].astype(str)
    df["ret_1"] = df["close"].pct_change()

    for code, bench in benchmarks.items():
        key = safe_code(code)
        b = bench[["trade_date", "close"]].rename(columns={"close": f"close_{key}"})
        b["trade_date"] = b["trade_date"].astype(str)
        df = df.merge(b, on="trade_date", how="left")

    df["ma50"] = df["close"].rolling(50, min_periods=50).mean()
    df["ma100"] = df["close"].rolling(100, min_periods=100).mean()
    df["ma150"] = df["close"].rolling(150, min_periods=100).mean()
    df["ma200"] = df["close"].rolling(200, min_periods=120).mean()
    df["ma250"] = df["close"].rolling(250, min_periods=150).mean()
    df["ma200_dev"] = df["close"] / df["ma200"] - 1
    df["rolling_high_3y"] = df["close"].rolling(756, min_periods=252).max()
    df["dd_3y"] = df["close"] / df["rolling_high_3y"] - 1
    df["price_pct_3y"] = rolling_percentile_last(df["close"], 756)
    df["rsi14"] = rsi(df["close"])

    for days in [20, 60, 120, 250]:
        df[f"ret_{days}"] = df["close"] / df["close"].shift(days) - 1

    for days in [20, 60, 250]:
        df[f"vol_{days}"] = df["ret_1"].rolling(days, min_periods=max(10, days // 2)).std() * math.sqrt(252)

    for code in benchmarks:
        key = safe_code(code)
        bench_ret = df[f"close_{key}"] / df[f"close_{key}"].shift(250) - 1
        df[f"rel_250_{key}"] = df["ret_250"] - bench_ret
    return df


def add_score(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    scored = df.copy()
    rows = []
    for spec in COMPONENTS:
        pct = expanding_percentile_last(scored[spec.key], min_periods=252)
        scored[f"{spec.key}_score"] = pct * 10
        scored[f"{spec.key}_contribution"] = scored[f"{spec.key}_score"] * spec.weight

    contribution_cols = [f"{spec.key}_contribution" for spec in COMPONENTS]
    scored["score"] = scored[contribution_cols].sum(axis=1, min_count=len(COMPONENTS))

    latest = scored.dropna(subset=["score"]).iloc[-1]
    for spec in COMPONENTS:
        rows.append(
            {
                "component": spec.label,
                "metric": spec.value_label,
                "value": latest[spec.key],
                "score": latest[f"{spec.key}_score"],
                "weight": spec.weight,
                "contribution": latest[f"{spec.key}_contribution"],
            }
        )
    return scored, pd.DataFrame(rows)


def add_forward_returns(df: pd.DataFrame, horizons: list[int]) -> pd.DataFrame:
    out = df.copy()
    for horizon in horizons:
        out[f"fwd_{horizon}"] = out["close"].shift(-horizon) / out["close"] - 1
    return out


def zone(score: float) -> str:
    if score <= 3:
        return "<=3 偏冷区"
    if score >= 7:
        return ">=7 偏热区"
    return "3-7 中性区"


def zone_backtest(scored: pd.DataFrame, horizons: list[int]) -> pd.DataFrame:
    rows = []
    valid = scored.dropna(subset=["score"]).copy()
    valid["zone"] = valid["score"].apply(zone)
    for horizon in horizons:
        col = f"fwd_{horizon}"
        tmp = valid.dropna(subset=[col])
        for z in ["<=3 偏冷区", "3-7 中性区", ">=7 偏热区"]:
            sub = tmp[tmp["zone"] == z]
            if sub.empty:
                rows.append({"horizon_days": horizon, "zone": z, "samples": 0})
                continue
            rows.append(
                {
                    "horizon_days": horizon,
                    "zone": z,
                    "samples": len(sub),
                    "win_rate": (sub[col] > 0).mean(),
                    "avg_return": sub[col].mean(),
                    "median_return": sub[col].median(),
                    "p10_return": sub[col].quantile(0.10),
                    "p90_return": sub[col].quantile(0.90),
                }
            )
    return pd.DataFrame(rows)


def years_between(dates: pd.Series) -> float:
    return max((dates.iloc[-1] - dates.iloc[0]).days / 365.25, 1 / 365.25)


def metric(frame: pd.DataFrame, signal: np.ndarray, cost: float) -> Metric:
    sig = pd.Series(signal, index=frame.index).ffill().fillna(1.0).clip(0.0, 1.0).to_numpy()
    pos = np.empty_like(sig, dtype=float)
    pos[0] = sig[0]
    pos[1:] = sig[:-1]
    prev = np.empty_like(pos)
    prev[0] = pos[0]
    prev[1:] = pos[:-1]
    turnover = np.abs(pos - prev)
    daily_ret = frame["daily_ret"].to_numpy()
    ret = pos * daily_ret - turnover * cost
    nav = np.cumprod(1.0 + ret)
    years = years_between(frame["date"])
    cagr = nav[-1] ** (1 / years) - 1
    dd = nav / np.maximum.accumulate(nav) - 1
    vol = np.nanstd(ret, ddof=1) * math.sqrt(252)
    return Metric(
        cagr=float(cagr),
        total_return=float(nav[-1] - 1),
        max_drawdown=float(np.nanmin(dd)),
        annual_vol=float(vol),
        sharpe=float(cagr / vol) if vol else np.nan,
        exposure=float(np.nanmean(pos)),
        turnover=float(np.nansum(turnover)),
        changes=int(np.nansum(turnover > 1e-12)),
        years=float(years),
    )


def segment_metric(df: pd.DataFrame, signal: np.ndarray, start: str, end: str | None, cost: float) -> Metric:
    mask = df["date"] >= pd.Timestamp(start)
    if end:
        mask &= df["date"] <= pd.Timestamp(end)
    sub = df.loc[mask].reset_index(drop=True)
    sub_signal = pd.Series(signal, index=df.index).loc[mask].reset_index(drop=True).to_numpy()
    return metric(sub, sub_signal, cost)


def rebalance_signal(df: pd.DataFrame, raw: np.ndarray | pd.Series, freq: str) -> np.ndarray:
    series = pd.Series(raw, index=df.index).ffill().fillna(1.0)
    if freq == "daily":
        return series.to_numpy()
    if freq == "weekly":
        keys = df["date"].dt.to_period("W-FRI")
    elif freq == "monthly":
        keys = df["date"].dt.to_period("M")
    else:
        raise ValueError(freq)
    rebal_idx = pd.DataFrame({"key": keys}, index=df.index).groupby("key").tail(1).index
    out = pd.Series(np.nan, index=df.index)
    out.loc[rebal_idx] = series.loc[rebal_idx]
    return out.ffill().fillna(series.iloc[0]).to_numpy()


def hysteresis_state(enter: pd.Series, exit_: pd.Series, initial: bool = True) -> np.ndarray:
    state = []
    full = initial
    for ent, ex in zip(enter.fillna(False), exit_.fillna(False)):
        if ent:
            full = True
        elif ex:
            full = False
        state.append(full)
    return np.array(state, dtype=float)


def generate_candidates(df: pd.DataFrame) -> list[tuple[str, np.ndarray, dict]]:
    candidates: list[tuple[str, np.ndarray, dict]] = []
    close = df["close"]
    score = df["score"]

    def add(name: str, raw: np.ndarray | pd.Series, meta: dict) -> None:
        raw_array = pd.Series(raw, index=df.index).ffill().fillna(1.0).clip(0.0, 1.0).to_numpy()
        candidates.append((name, raw_array, meta))

    add("buy_hold", np.ones(len(df)), {"family": "baseline"})

    for base in [0.3, 0.4, 0.5, 0.6, 0.7]:
        for window in [50, 100, 150, 200, 250]:
            ma = df[f"ma{window if window in [50, 100, 150, 200, 250] else 200}"]
            for low in [3.0, 3.5, 4.0, 4.5]:
                raw = base + (1 - base) * ((close > ma) | (score <= low)).astype(float)
                for freq in ["daily", "weekly", "monthly"]:
                    add(
                        f"{freq}_trend_low_base{base:.1f}_ma{window}_low{low:.1f}",
                        rebalance_signal(df, raw, freq),
                        {"family": "trend_low", "base": base, "ma": window, "low_score": low, "freq": freq},
                    )

    for base in [0.3, 0.4, 0.5, 0.6]:
        for window in [50, 100, 150, 200, 250]:
            ma = df[f"ma{window}"]
            for low in [3.0, 3.5, 4.0, 4.5]:
                for band in [0.01, 0.02, 0.03, 0.05]:
                    enter = (close > ma * (1 + band)) | (score <= low)
                    exit_ = (close < ma * (1 - band)) & (score > low)
                    raw = base + (1 - base) * hysteresis_state(enter, exit_)
                    for freq in ["daily", "weekly", "monthly"]:
                        add(
                            f"{freq}_band_base{base:.1f}_ma{window}_low{low:.1f}_band{band:.2f}",
                            rebalance_signal(df, raw, freq),
                            {
                                "family": "band",
                                "base": base,
                                "ma": window,
                                "low_score": low,
                                "band": band,
                                "freq": freq,
                            },
                        )

    for base in [0.3, 0.4, 0.5, 0.6, 0.7]:
        for high in [5.5, 6.0, 6.5, 7.0, 7.5, 8.0]:
            raw = base + (1 - base) * (score < high).astype(float)
            for freq in ["daily", "weekly", "monthly"]:
                add(
                    f"{freq}_score_only_base{base:.1f}_high{high:.1f}",
                    rebalance_signal(df, raw, freq),
                    {"family": "score_only", "base": base, "high_score": high, "freq": freq},
                )

    for base in [0.3, 0.5, 0.7]:
        for window in [100, 150, 200, 250]:
            ma = df[f"ma{window}"]
            for high in [6.5, 7.0, 7.5, 8.0]:
                raw = np.where((score >= high) & (close < ma), base, 1.0)
                for freq in ["daily", "weekly", "monthly"]:
                    add(
                        f"{freq}_risk_off_high_base{base:.1f}_ma{window}_high{high:.1f}",
                        rebalance_signal(df, raw, freq),
                        {"family": "risk_off_high", "base": base, "ma": window, "high_score": high, "freq": freq},
                    )
    return candidates


def metric_row(prefix: str, value: Metric) -> dict[str, float | int]:
    return {
        f"{prefix}_cagr": value.cagr,
        f"{prefix}_mdd": value.max_drawdown,
        f"{prefix}_sharpe": value.sharpe,
        f"{prefix}_exposure": value.exposure,
        f"{prefix}_changes": value.changes,
    }


def research_strategies(scored: pd.DataFrame, cost: float) -> tuple[pd.DataFrame, dict, np.ndarray]:
    df = scored.sort_values("trade_date").dropna(subset=["close", "score"]).reset_index(drop=True)
    df["date"] = pd.to_datetime(df["trade_date"])
    df["daily_ret"] = df["close"].pct_change().fillna(0.0)

    candidates = generate_candidates(df)
    buyhold_signal = np.ones(len(df))
    buyhold = metric(df, buyhold_signal, 0.0)

    rows = []
    signal_map = {}
    for name, signal, meta in candidates:
        use_cost = 0.0 if name == "buy_hold" else cost
        full = metric(df, signal, use_cost)
        train = segment_metric(df, signal, "2007-01-01", "2015-12-31", use_cost)
        valid = segment_metric(df, signal, "2016-01-01", "2021-12-31", use_cost)
        test = segment_metric(df, signal, "2022-01-01", None, use_cost)
        recent = segment_metric(df, signal, "2020-01-01", None, use_cost)
        mdd_improvement = full.max_drawdown - buyhold.max_drawdown
        utility = full.cagr + 0.55 * mdd_improvement + 0.03 * full.sharpe - 0.002 * (full.turnover / full.years)
        row = {
            "name": name,
            **meta,
            "cagr": full.cagr,
            "total_return": full.total_return,
            "max_drawdown": full.max_drawdown,
            "mdd_improvement_vs_buyhold": mdd_improvement,
            "annual_vol": full.annual_vol,
            "sharpe": full.sharpe,
            "exposure": full.exposure,
            "turnover": full.turnover,
            "turnover_per_year": full.turnover / full.years,
            "changes": full.changes,
            "utility": utility,
            **metric_row("train", train),
            **metric_row("valid", valid),
            **metric_row("test2022", test),
            **metric_row("recent2020", recent),
        }
        rows.append(row)
        signal_map[name] = signal

    grid = pd.DataFrame(rows).sort_values("utility", ascending=False).reset_index(drop=True)
    robust = grid[
        (grid["name"] != "buy_hold")
        & (grid["cagr"] >= buyhold.cagr - 0.015)
        & (grid["mdd_improvement_vs_buyhold"] >= 0.04)
        & (grid["test2022_cagr"] > 0)
        & (grid["valid_cagr"] > 0)
    ].copy()
    selected = robust.iloc[0].to_dict() if not robust.empty else grid[grid["name"] != "buy_hold"].iloc[0].to_dict()
    selected_signal = signal_map[str(selected["name"])]
    summary = {
        "cost": cost,
        "buyhold": buyhold.__dict__,
        "selected": selected,
        "robust_candidates": int(len(robust)),
        "total_candidates": int(len(grid)),
    }
    return grid, summary, selected_signal


def describe_strategy(selected: dict) -> str:
    family = selected.get("family")
    freq = str(selected.get("freq", "daily"))
    freq_text = {"daily": "每日", "weekly": "每周", "monthly": "每月"}.get(freq, freq)
    base = selected.get("base")
    ma = selected.get("ma")
    low_score = selected.get("low_score")
    band = selected.get("band")
    high_score = selected.get("high_score")

    if family == "band":
        return (
            f"{freq_text}确认一次目标仓位；若收盘价站上 MA{int(ma)} 上轨 {1 + float(band):.2f} 倍，"
            f"或评分 <= {float(low_score):.1f}，目标仓位为 100%；"
            f"若收盘价跌破 MA{int(ma)} 下轨 {1 - float(band):.2f} 倍且评分 > {float(low_score):.1f}，"
            f"目标仓位降至 {pct(float(base))}；其余时间保持上一目标仓位。"
        )
    if family == "trend_low":
        return (
            f"{freq_text}确认一次目标仓位；若收盘价在 MA{int(ma)} 之上，或评分 <= {float(low_score):.1f}，"
            f"目标仓位为 100%；否则目标仓位为 {pct(float(base))}。"
        )
    if family == "risk_off_high":
        return (
            f"{freq_text}确认一次目标仓位；若评分 >= {float(high_score):.1f} 且收盘价低于 MA{int(ma)}，"
            f"目标仓位降至 {pct(float(base))}；否则保持 100%。"
        )
    if family == "score_only":
        return (
            f"{freq_text}确认一次目标仓位；若评分低于 {float(high_score):.1f}，目标仓位为 100%；"
            f"否则目标仓位为 {pct(float(base))}。"
        )
    return str(selected.get("name", "unknown"))


def build_nav(scored: pd.DataFrame, signal: np.ndarray, cost: float) -> pd.DataFrame:
    df = scored.sort_values("trade_date").dropna(subset=["close", "score"]).reset_index(drop=True)
    df["date"] = pd.to_datetime(df["trade_date"])
    df["daily_ret"] = df["close"].pct_change().fillna(0.0)
    df["target_position"] = pd.Series(signal, index=df.index).ffill().fillna(1.0).clip(0.0, 1.0)
    df["position"] = df["target_position"].shift(1).fillna(df["target_position"].iloc[0])
    df["turnover"] = (df["position"] - df["position"].shift(1).fillna(df["position"].iloc[0])).abs()
    df["strategy_ret"] = df["position"] * df["daily_ret"] - df["turnover"] * cost
    df["buyhold_ret"] = df["daily_ret"]
    df["strategy_nav"] = (1 + df["strategy_ret"]).cumprod()
    df["buyhold_nav"] = (1 + df["buyhold_ret"]).cumprod()
    return df


def summary_stats(df: pd.DataFrame) -> dict[str, float | str]:
    valid = df.dropna(subset=["close"]).copy()
    start = valid.iloc[0]
    latest = valid.iloc[-1]
    years = len(valid) / 252
    total_ret = latest["close"] / start["close"] - 1
    cagr = (latest["close"] / start["close"]) ** (1 / years) - 1
    vol = valid["ret_1"].std() * math.sqrt(252)
    max_dd = (valid["close"] / valid["close"].cummax() - 1).min()
    current_dd = latest["close"] / valid["close"].cummax().iloc[-1] - 1
    return {
        "start_date": start["trade_date"],
        "latest_date": latest["trade_date"],
        "latest_close": latest["close"],
        "total_return": total_ret,
        "cagr": cagr,
        "ann_vol": vol,
        "sharpe_0rf": cagr / vol if vol else np.nan,
        "max_drawdown": max_dd,
        "current_drawdown": current_dd,
        "ret_20": latest.get("ret_20"),
        "ret_60": latest.get("ret_60"),
        "ret_120": latest.get("ret_120"),
        "ret_250": latest.get("ret_250"),
    }


def pct(value: float | None) -> str:
    if value is None or not np.isfinite(value):
        return "-"
    return f"{value * 100:.2f}%"


def num(value: float | None, digits: int = 2) -> str:
    if value is None or not np.isfinite(value):
        return "-"
    return f"{value:.{digits}f}"


def ymd(value: str) -> str:
    text = str(value)
    if len(text) == 8:
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return text


def markdown_table(df: pd.DataFrame, columns: list[str]) -> str:
    rows = ["|" + "|".join(columns) + "|", "|" + "|".join(["---"] * len(columns)) + "|"]
    for _, row in df.iterrows():
        rows.append("|" + "|".join(str(row.get(col, "")) for col in columns) + "|")
    return "\n".join(rows)


def format_component_value(metric_name: str, value: float) -> str:
    if metric_name in {"近3年价格分位", "RSI14"}:
        return num(value, 2)
    if "涨幅" in metric_name or "波动" in metric_name or "回撤" in metric_name or "均线" in metric_name or "相对" in metric_name:
        return pct(value)
    return num(value, 2)


def write_outputs(
    scored: pd.DataFrame,
    components: pd.DataFrame,
    zone_bt: pd.DataFrame,
    grid: pd.DataFrame,
    summary: dict,
    nav: pd.DataFrame,
    stats: dict,
    cost: float,
) -> Path:
    ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)
    latest = scored.dropna(subset=["score"]).iloc[-1]
    selected = summary["selected"]
    selected_rule = describe_strategy(selected)

    scored.to_csv(ANALYSIS_DIR / "nasdaq100_score_history.csv", index=False, encoding="utf-8-sig")
    components.to_csv(ANALYSIS_DIR / "nasdaq100_score_components_latest.csv", index=False, encoding="utf-8-sig")
    zone_bt.to_csv(ANALYSIS_DIR / "nasdaq100_score_zone_backtest.csv", index=False, encoding="utf-8-sig")
    grid.to_csv(ANALYSIS_DIR / "nasdaq100_strategy_research_grid.csv", index=False, encoding="utf-8-sig")
    nav.to_csv(ANALYSIS_DIR / "nasdaq100_recommended_strategy_nav.csv", index=False, encoding="utf-8-sig")
    (ANALYSIS_DIR / "nasdaq100_strategy_research_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    latest_json = {
        "index_code": TARGET[1],
        "latest_date": latest["trade_date"],
        "latest_close": float(latest["close"]),
        "score": float(latest["score"]),
        "zone": zone(float(latest["score"])),
        "recommended_strategy": selected["name"],
        "target_position": float(nav.iloc[-1]["target_position"]),
        "applied_position": float(nav.iloc[-1]["position"]),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    (ANALYSIS_DIR / "nasdaq100_latest_signal.json").write_text(
        json.dumps(latest_json, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    buyhold = summary["buyhold"]
    strategy_metric = metric(nav, nav["target_position"].to_numpy(), cost).__dict__
    top_grid = grid.head(15).copy()
    top_grid["cagr_fmt"] = top_grid["cagr"].map(pct)
    top_grid["mdd_fmt"] = top_grid["max_drawdown"].map(pct)
    top_grid["mdd_improve_fmt"] = top_grid["mdd_improvement_vs_buyhold"].map(pct)
    top_grid["test2022_cagr_fmt"] = top_grid["test2022_cagr"].map(pct)
    top_grid["exposure_fmt"] = top_grid["exposure"].map(pct)

    zone_fmt = zone_bt.copy()
    for col in ["win_rate", "avg_return", "median_return", "p10_return", "p90_return"]:
        zone_fmt[col] = zone_fmt[col].map(pct)

    comp_fmt = components.copy()
    comp_fmt["value"] = comp_fmt.apply(lambda r: format_component_value(r["metric"], r["value"]), axis=1)
    comp_fmt["score"] = comp_fmt["score"].map(lambda x: num(x, 2))
    comp_fmt["weight"] = comp_fmt["weight"].map(pct)
    comp_fmt["contribution"] = comp_fmt["contribution"].map(lambda x: num(x, 2))

    report = f"""# 纳斯达克100系统化评分与策略研究

生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

## 当前结论

- 指数：纳斯达克100，数据源 Yahoo Finance `^NDX`
- 最新交易日：{ymd(str(latest['trade_date']))}
- 最新收盘点位：{num(float(latest['close']), 2)}
- 当前评分：**{num(float(latest['score']), 2)} / 10**
- 当前区间：**{zone(float(latest['score']))}**
- 推荐行动策略：`{selected['name']}`
- 推荐规则：{selected_rule}
- 当前目标仓位：**{pct(float(nav.iloc[-1]['target_position']))}**
- 当前执行仓位：{pct(float(nav.iloc[-1]['position']))}

评分含义：0 分附近代表相对历史偏低、偏冷或回撤较深；10 分附近代表相对历史偏高、偏热或拥挤。评分回测使用滚动历史分位，避免用未来样本给过去打分。

## 指数长期表现

- 样本区间：{ymd(str(stats['start_date']))} 至 {ymd(str(stats['latest_date']))}
- 累计收益：{pct(stats['total_return'])}
- 年化收益：{pct(stats['cagr'])}
- 年化波动：{pct(stats['ann_vol'])}
- 0利率夏普：{num(stats['sharpe_0rf'], 2)}
- 历史最大回撤：{pct(stats['max_drawdown'])}
- 当前距历史高点回撤：{pct(stats['current_drawdown'])}
- 近20/60/120/250日涨幅：{pct(stats['ret_20'])} / {pct(stats['ret_60'])} / {pct(stats['ret_120'])} / {pct(stats['ret_250'])}

## 打分方法

总分 = 各指标滚动历史分位子分 * 权重后求和。子分越高代表越偏热。

{markdown_table(comp_fmt, ['component', 'metric', 'value', 'score', 'weight', 'contribution'])}

## 评分区间历史表现

{markdown_table(zone_fmt, ['horizon_days', 'zone', 'samples', 'win_rate', 'avg_return', 'median_return', 'p10_return', 'p90_return'])}

## 策略搜索方法

搜索范围包含三类可解释规则：

- 趋势 + 低分保护：站上均线或评分偏低时满仓，否则保留底仓。
- 均线通道：站上均线上轨或评分偏低时满仓，跌破下轨且评分不低时降到底仓。
- 高分风险控制：评分偏热且跌破长期均线时降仓。

共同假设：只做多、不加杠杆，信号收盘后确认，下一交易日执行，仓位变化成本 {pct(cost)}。

## 推荐策略回测

- 策略名称：`{selected['name']}`
- 策略规则：{selected_rule}
- 候选策略数：{summary['total_candidates']}
- 稳健筛选通过数量：{summary['robust_candidates']}
- 策略年化收益：{pct(strategy_metric['cagr'])}
- 买入持有年化收益：{pct(buyhold['cagr'])}
- 策略累计收益：{pct(strategy_metric['total_return'])}
- 买入持有累计收益：{pct(buyhold['total_return'])}
- 策略最大回撤：{pct(strategy_metric['max_drawdown'])}
- 买入持有最大回撤：{pct(buyhold['max_drawdown'])}
- 策略年化波动：{pct(strategy_metric['annual_vol'])}
- 买入持有年化波动：{pct(buyhold['annual_vol'])}
- 平均仓位：{pct(strategy_metric['exposure'])}
- 仓位变化：{int(strategy_metric['changes'])} 次
- 年均换手：{num(strategy_metric['turnover'] / strategy_metric['years'], 2)} 倍目标资金

## 候选策略前十五

{markdown_table(top_grid, ['name', 'cagr_fmt', 'mdd_fmt', 'mdd_improve_fmt', 'test2022_cagr_fmt', 'exposure_fmt', 'changes'])}

## 使用提醒

纳斯达克100是长期高成长、高波动资产。历史上很多降仓策略会显著降低回撤，但也可能牺牲牛市收益。这里的推荐策略更适合作为“目标仓位提示”，不适合机械预测短期涨跌。若用 QQQ 或境内纳指ETF执行，还需要额外考虑汇率、交易时间差、溢价率、费率和税务。
"""
    report_path = ANALYSIS_DIR / "nasdaq100_strategy_report.md"
    report_path.write_text(report, encoding="utf-8")
    return report_path


def main() -> int:
    args = parse_args()
    target = fetch_yahoo_daily(TARGET[0], TARGET[1], args.start_date, args.end_date, args.refresh)
    benchmarks = {
        code: fetch_yahoo_daily(symbol, code, args.start_date, args.end_date, args.refresh)
        for symbol, code, _ in BENCHMARKS
    }
    indicators = build_indicator_frame(target, benchmarks)
    scored, components = add_score(indicators)
    scored = add_forward_returns(scored, [60, 120, 250])
    zone_bt = zone_backtest(scored, [60, 120, 250])
    grid, summary, selected_signal = research_strategies(scored, args.cost)
    nav = build_nav(scored, selected_signal, args.cost)
    stats = summary_stats(scored)
    report_path = write_outputs(scored, components, zone_bt, grid, summary, nav, stats, args.cost)

    latest = scored.dropna(subset=["score"]).iloc[-1]
    selected = summary["selected"]
    buyhold = summary["buyhold"]
    selected_metric = metric(nav, nav["target_position"].to_numpy(), args.cost)
    print(f"Report: {report_path}")
    print(f"Latest date: {latest['trade_date']}")
    print(f"Latest close: {latest['close']:.2f}")
    print(f"Score: {latest['score']:.2f}/10 ({zone(float(latest['score']))})")
    print(f"Selected strategy: {selected['name']}")
    print(f"Target position: {nav.iloc[-1]['target_position']:.2%}")
    print(f"Strategy CAGR: {selected_metric.cagr:.2%}; buyhold CAGR: {buyhold['cagr']:.2%}")
    print(f"Strategy MDD: {selected_metric.max_drawdown:.2%}; buyhold MDD: {buyhold['max_drawdown']:.2%}")
    print("Score zone backtest:")
    print(zone_bt.to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
