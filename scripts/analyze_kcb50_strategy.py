#!/usr/bin/env python3
"""Analyze STAR 50 (000688.SH) score model and long-only action strategies."""

from __future__ import annotations

import argparse
import bisect
import json
import math
import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import tushare as ts
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parents[1]
RAW_INDEX_DIR = BASE_DIR / "data" / "raw" / "index_daily"
ANALYSIS_DIR = BASE_DIR / "analysis" / "kcb50"

TARGET = "000688.SH"
TARGET_NAME = "科创50"
START_DATE = "20191231"
BENCHMARKS = {
    "000300.SH": "沪深300",
    "399006.SZ": "创业板指",
}

DEFAULT_COST = 0.001
DEFAULT_CASH_RETURN = 0.02
MIN_SCORE_OBSERVATIONS = 20
MIN_TARGET_CAGR = 0.15
MAX_ACCEPTABLE_DRAWDOWN = -0.75
MAX_TURNOVER_PER_YEAR = 12.0
MAX_CHANGES_PER_YEAR = 24.0


@dataclass(frozen=True)
class ComponentSpec:
    key: str
    label: str
    weight: float
    value_label: str
    higher_is_hot: bool = True


COMPONENTS = [
    ComponentSpec("price_pct_60", "三月价格位置", 0.12, "近60日价格分位"),
    ComponentSpec("dd_60", "三月回撤修复", 0.10, "距60日高点回撤"),
    ComponentSpec("ma20_dev", "短期均线偏离", 0.10, "相对20日均线"),
    ComponentSpec("rsi14", "短期拥挤度", 0.08, "RSI14"),
    ComponentSpec("ret_10", "两周涨幅", 0.08, "10日涨幅"),
    ComponentSpec("ret_15", "三周涨幅", 0.10, "15日涨幅"),
    ComponentSpec("ret_20", "一月涨幅", 0.12, "20日涨幅"),
    ComponentSpec("vol_20", "短期波动风险", 0.10, "20日年化波动"),
    ComponentSpec("rel_20_000300_SH", "相对沪深300热度", 0.10, "20日相对沪深300"),
    ComponentSpec("rel_20_399006_SZ", "相对创业板热度", 0.10, "20日相对创业板"),
]


@dataclass
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build STAR 50 score and strategy research report.")
    parser.add_argument("--start-date", default=START_DATE)
    parser.add_argument("--end-date", default=datetime.now().strftime("%Y%m%d"))
    parser.add_argument("--refresh", action="store_true", help="Refetch cached index data.")
    parser.add_argument("--intraday", action="store_true", help="Append realtime index quotes for the current trading day.")
    parser.add_argument("--cost", type=float, default=DEFAULT_COST)
    parser.add_argument("--cash-return", type=float, default=DEFAULT_CASH_RETURN)
    return parser.parse_args()


def init_tushare():
    load_dotenv(BASE_DIR / ".env")
    token = os.getenv("TUSHARE_TOKEN")
    if not token:
        raise SystemExit("Missing TUSHARE_TOKEN")
    return ts.pro_api(token)


def safe_code(code: str) -> str:
    return code.replace(".", "_").replace("/", "_")


def fetch_index_daily(pro, ts_code: str, start_date: str, end_date: str, refresh: bool) -> pd.DataFrame:
    RAW_INDEX_DIR.mkdir(parents=True, exist_ok=True)
    cache = RAW_INDEX_DIR / f"{safe_code(ts_code)}_{start_date}_{end_date}.parquet"
    if cache.exists() and not refresh:
        return pd.read_parquet(cache)

    df = pro.index_daily(ts_code=ts_code, start_date=start_date, end_date=end_date)
    if df.empty:
        raise RuntimeError(f"No index_daily data returned for {ts_code}")
    df = df.sort_values("trade_date").reset_index(drop=True)
    df.to_parquet(cache, index=False)
    return df


def apply_intraday_quotes(target: pd.DataFrame, benchmarks: dict[str, pd.DataFrame]) -> tuple[pd.DataFrame, dict[str, pd.DataFrame], dict]:
    codes = [TARGET, *BENCHMARKS]
    quotes = ts.realtime_quote(ts_code=",".join(codes))
    if quotes.empty:
        raise RuntimeError("No realtime index quotes returned.")
    quotes.columns = [str(col).upper() for col in quotes.columns]
    quotes = quotes.set_index("TS_CODE", drop=False)
    missing = [code for code in codes if code not in quotes.index]
    if missing:
        raise RuntimeError(f"Realtime quotes missing for: {', '.join(missing)}")

    def as_float(row: pd.Series, key: str) -> float:
        value = pd.to_numeric(pd.Series([row.get(key)]), errors="coerce").iloc[0]
        if not np.isfinite(value):
            raise RuntimeError(f"Realtime quote {row.get('TS_CODE')} has invalid {key}: {row.get(key)}")
        return float(value)

    def append_quote(frame: pd.DataFrame, code: str) -> pd.DataFrame:
        row = quotes.loc[code]
        trade_date = str(row.get("DATE"))
        price = as_float(row, "PRICE")
        pre_close = as_float(row, "PRE_CLOSE")
        if price <= 0 or pre_close <= 0:
            raise RuntimeError(f"Realtime quote {code} has invalid price/pre_close.")
        new_row = {col: np.nan for col in frame.columns}
        new_row.update(
            {
                "trade_date": trade_date,
                "open": as_float(row, "OPEN"),
                "close": price,
                "high": as_float(row, "HIGH"),
                "low": as_float(row, "LOW"),
                "pct_chg": (price / pre_close - 1) * 100,
                "vol": as_float(row, "VOLUME"),
            }
        )
        out = frame[frame["trade_date"].astype(str) != trade_date].copy()
        out = pd.concat([out, pd.DataFrame([new_row])], ignore_index=True)
        return out.sort_values("trade_date").reset_index(drop=True)

    updated_benchmarks = {code: append_quote(frame, code) for code, frame in benchmarks.items()}
    target_row = quotes.loc[TARGET]
    meta = {
        "data_source": "realtime_quote",
        "quote_date": str(target_row.get("DATE")),
        "quote_time": str(target_row.get("TIME")),
        "quote_price": as_float(target_row, "PRICE"),
    }
    return append_quote(target, TARGET), updated_benchmarks, meta


def rsi(series: pd.Series, window: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / window, adjust=False, min_periods=window).mean()
    avg_loss = loss.ewm(alpha=1 / window, adjust=False, min_periods=window).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def rolling_percentile_last(series: pd.Series, window: int, min_periods: int) -> pd.Series:
    def calc(values: np.ndarray) -> float:
        current = values[-1]
        if not np.isfinite(current):
            return np.nan
        valid = values[np.isfinite(values)]
        if len(valid) == 0:
            return np.nan
        return float((valid <= current).sum() / len(valid))

    return series.rolling(window, min_periods=min_periods).apply(calc, raw=True)


def expanding_percentile(series: pd.Series, min_periods: int = MIN_SCORE_OBSERVATIONS) -> pd.Series:
    values: list[float] = []
    out = np.full(len(series), np.nan, dtype=float)
    for i, value in enumerate(series.to_numpy(dtype=float)):
        if not np.isfinite(value):
            continue
        bisect.insort(values, float(value))
        if len(values) < min_periods:
            continue
        left = bisect.bisect_left(values, float(value))
        right = bisect.bisect_right(values, float(value))
        average_rank = (left + 1 + right) / 2
        out[i] = average_rank / len(values)
    return pd.Series(out, index=series.index)


def build_indicator_frame(target: pd.DataFrame, benchmarks: dict[str, pd.DataFrame]) -> pd.DataFrame:
    df = target[["trade_date", "open", "close", "high", "low", "pct_chg", "vol"]].copy()
    df["trade_date"] = df["trade_date"].astype(str)
    df["ret_1"] = df["close"].pct_change()

    for code, bench in benchmarks.items():
        key = safe_code(code)
        b = bench[["trade_date", "close"]].rename(columns={"close": f"close_{key}"})
        b["trade_date"] = b["trade_date"].astype(str)
        df = df.merge(b, on="trade_date", how="left")

    for window in [10, 15, 20, 25, 30, 40, 50, 60, 80, 100, 120, 150, 200, 250]:
        df[f"ma{window}"] = df["close"].rolling(window, min_periods=min(window, max(10, window // 2))).mean()
    df["ma20_dev"] = df["close"] / df["ma20"] - 1
    df["ma200_dev"] = df["close"] / df["ma200"] - 1
    df["rolling_high_60"] = df["close"].rolling(60, min_periods=20).max()
    df["dd_60"] = df["close"] / df["rolling_high_60"] - 1
    df["price_pct_60"] = rolling_percentile_last(df["close"], 60, 20)
    df["rolling_high_2y"] = df["close"].rolling(500, min_periods=160).max()
    df["dd_2y"] = df["close"] / df["rolling_high_2y"] - 1
    df["price_pct_2y"] = rolling_percentile_last(df["close"], 500, 160)
    df["rsi14"] = rsi(df["close"])

    for days in [5, 10, 15, 20, 25, 30, 40, 50, 60, 80, 100, 120, 250]:
        df[f"ret_{days}"] = df["close"] / df["close"].shift(days) - 1
    for days in [10, 20, 60, 120, 250]:
        df[f"vol_{days}"] = df["ret_1"].rolling(days, min_periods=max(10, days // 2)).std() * math.sqrt(252)

    for code in benchmarks:
        key = safe_code(code)
        bench_ret = df[f"close_{key}"] / df[f"close_{key}"].shift(20) - 1
        df[f"rel_20_{key}"] = df["ret_20"] - bench_ret
        bench_ret = df[f"close_{key}"] / df[f"close_{key}"].shift(120) - 1
        df[f"rel_120_{key}"] = df["ret_120"] - bench_ret
    return df


def add_score(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    scored = df.copy()
    rows = []
    for spec in COMPONENTS:
        pct = expanding_percentile(scored[spec.key])
        score = pct * 10 if spec.higher_is_hot else (1 - pct) * 10
        scored[f"{spec.key}_score"] = score
        scored[f"{spec.key}_contribution"] = score * spec.weight

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
        return "<=3 低温/左侧观察区"
    if score >= 7:
        return ">=7 高温/拥挤风险区"
    return "3-7 中性区"


def zone_backtest(scored: pd.DataFrame, horizons: list[int]) -> pd.DataFrame:
    rows = []
    valid = scored.dropna(subset=["score"]).copy()
    valid["zone"] = valid["score"].apply(zone)
    for horizon in horizons:
        col = f"fwd_{horizon}"
        tmp = valid.dropna(subset=[col])
        for z in ["<=3 低温/左侧观察区", "3-7 中性区", ">=7 高温/拥挤风险区"]:
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
    return (dates.iloc[-1] - dates.iloc[0]).days / 365.25


def compute_metric(
    frame: pd.DataFrame,
    signal: np.ndarray,
    cost: float,
    cash_return: float,
) -> Metric:
    sig = pd.Series(signal, index=frame.index).ffill().fillna(1.0).clip(0.0, 1.0).to_numpy()
    pos = np.empty_like(sig, dtype=float)
    pos[0] = sig[0]
    pos[1:] = sig[:-1]
    prev = np.empty_like(pos)
    prev[0] = pos[0]
    prev[1:] = pos[:-1]
    turnover = np.abs(pos - prev)
    cash_daily = (1.0 + cash_return) ** (1 / 252) - 1
    ret = pos * frame["daily_ret"].to_numpy() + (1 - pos) * cash_daily - turnover * cost
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


def compute_segment_metric(
    df: pd.DataFrame,
    signal: np.ndarray,
    start: str,
    end: str | None,
    cost: float,
    cash_return: float,
) -> Metric | None:
    mask = df["date"] >= pd.Timestamp(start)
    if end:
        mask &= df["date"] <= pd.Timestamp(end)
    if int(mask.sum()) < 30:
        return None
    sub = df.loc[mask].reset_index(drop=True)
    sub_signal = pd.Series(signal, index=df.index).loc[mask].reset_index(drop=True).to_numpy()
    return compute_metric(sub, sub_signal, cost=cost, cash_return=cash_return)


def rebalance_signal(df: pd.DataFrame, raw: np.ndarray, freq: str) -> np.ndarray:
    series = pd.Series(raw, index=df.index).ffill().fillna(1.0).clip(0, 1)
    if freq == "daily":
        return series.to_numpy()
    if freq == "weekly":
        keys = df["date"].dt.to_period("W-FRI")
        latest_is_period_end = df["date"].iloc[-1].weekday() == 4
    elif freq == "monthly":
        keys = df["date"].dt.to_period("M")
        latest = df["date"].iloc[-1]
        latest_is_period_end = bool(latest.is_month_end or pd.offsets.BMonthEnd().is_on_offset(latest))
    else:
        raise ValueError(freq)
    rebal_idx = pd.DataFrame({"key": keys}, index=df.index).groupby("key").tail(1).index
    if len(rebal_idx) and rebal_idx[-1] == df.index[-1] and not latest_is_period_end:
        rebal_idx = rebal_idx[:-1]
    out = pd.Series(np.nan, index=df.index)
    out.loc[rebal_idx] = series.loc[rebal_idx]
    return out.ffill().fillna(1.0).to_numpy()


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

    def add(name: str, raw: np.ndarray | pd.Series, meta: dict, freqs: tuple[str, ...] = ("daily", "weekly", "monthly")) -> None:
        raw_array = pd.Series(raw, index=df.index).ffill().fillna(1.0).clip(0, 1).to_numpy()
        for freq in freqs:
            candidates.append((f"{freq}_{name}", rebalance_signal(df, raw_array, freq), {**meta, "freq": freq}))

    candidates.append(("buy_hold", np.ones(len(df)), {"family": "baseline", "freq": "daily"}))

    for base in [0.0, 0.1, 0.2, 0.3, 0.4]:
        for window in [10, 15, 20, 25, 30, 40, 50, 60, 80, 120, 150, 200, 250]:
            trend = close > df[f"ma{window}"]
            for low in [2.5, 3.0, 3.5, 4.0, 4.5]:
                raw = base + (1 - base) * ((trend | (score <= low)).astype(float))
                add(
                    f"trend_low_base{base:.1f}_ma{window}_low{low:.1f}",
                    raw,
                    {"family": "trend_low", "base": base, "ma": window, "low_score": low},
                )

            for high in [5.0, 5.5, 6.0, 6.5, 7.0, 7.5]:
                raw = np.where((score >= high) & (~trend), base, 1.0)
                add(
                    f"risk_off_high_base{base:.1f}_ma{window}_high{high:.1f}",
                    raw,
                    {"family": "risk_off_high", "base": base, "ma": window, "high_score": high},
                )

            for low in [3.0, 3.5, 4.0, 4.5]:
                for high in [5.5, 6.0, 6.5, 7.0]:
                    raw = base + (1 - base) * (((trend & (score < high)) | (score <= low)).astype(float))
                    add(
                        f"trend_score_base{base:.1f}_ma{window}_low{low:.1f}_high{high:.1f}",
                        raw,
                        {"family": "trend_score", "base": base, "ma": window, "low_score": low, "high_score": high},
                    )

    for base in [0.0, 0.1, 0.2, 0.3, 0.4]:
        for window in [10, 15, 20, 25, 30, 40, 50, 60, 80, 120, 150, 200]:
            ma = df[f"ma{window}"]
            for low in [3.0, 3.5, 4.0]:
                for band in [0.0, 0.02, 0.04, 0.06, 0.08]:
                    enter = (close > ma * (1 + band)) | (score <= low)
                    exit_ = (close < ma * (1 - band)) & (score > low)
                    raw_state = hysteresis_state(enter, exit_)
                    raw = base + (1 - base) * raw_state
                    add(
                        f"band_base{base:.1f}_ma{window}_low{low:.1f}_band{band:.2f}",
                        raw,
                        {"family": "band", "base": base, "ma": window, "low_score": low, "band": band},
                    )

    for base in [0.0, 0.05, 0.1, 0.15, 0.2, 0.3]:
        for ret_window in [5, 10, 15, 20, 25, 30, 40, 50, 60, 80, 120, 250]:
            ret = df[f"ret_{ret_window}"]
            for threshold in [-0.05, 0.0, 0.02, 0.04, 0.05, 0.08, 0.10, 0.15]:
                raw = base + (1 - base) * (ret > threshold).astype(float)
                add(
                    f"pure_momentum_base{base:.2f}_ret{ret_window}_th{threshold:.2f}",
                    raw,
                    {"family": "pure_momentum", "base": base, "ret_window": ret_window, "threshold": threshold},
                )
                for low in [3.0, 3.5, 4.0]:
                    raw = base + (1 - base) * (((ret > threshold) | (score <= low)).astype(float))
                    add(
                        f"momentum_base{base:.1f}_ret{ret_window}_th{threshold:.2f}_low{low:.1f}",
                        raw,
                        {"family": "momentum", "base": base, "ret_window": ret_window, "threshold": threshold, "low_score": low},
                    )

    for base in [0.0, 0.1, 0.2, 0.3, 0.4]:
        for high in [4.5, 5.0, 5.5, 6.0, 6.5, 7.0, 7.5]:
            raw = base + (1 - base) * (score < high).astype(float)
            add(
                f"score_only_base{base:.1f}_high{high:.1f}",
                raw,
                {"family": "score_only", "base": base, "high_score": high},
            )
    return candidates


def target_pass(row: dict) -> bool:
    return (
        row["cagr"] >= MIN_TARGET_CAGR
        and row["max_drawdown"] >= MAX_ACCEPTABLE_DRAWDOWN
        and row["turnover_per_year"] <= MAX_TURNOVER_PER_YEAR
        and row["changes_per_year"] <= MAX_CHANGES_PER_YEAR
        and row["valid_cagr"] > 0
        and row["test_cagr"] > 0
        and row["min_position"] >= -1e-12
        and row["max_position"] <= 1 + 1e-12
    )


def research_strategies(scored: pd.DataFrame, cost: float, cash_return: float) -> tuple[pd.DataFrame, dict, np.ndarray]:
    df = scored.sort_values("trade_date").dropna(subset=["close", "score"]).reset_index(drop=True)
    df["date"] = pd.to_datetime(df["trade_date"])
    df["daily_ret"] = df["close"].pct_change().fillna(0.0)
    candidates = generate_candidates(df)
    benchmark_signal = np.ones(len(df))
    benchmark = compute_metric(df, benchmark_signal, cost=0.0, cash_return=0.0)
    benchmark_valid = compute_segment_metric(df, benchmark_signal, "2022-01-01", "2023-12-31", cost=0.0, cash_return=0.0)
    benchmark_test = compute_segment_metric(df, benchmark_signal, "2024-01-01", None, cost=0.0, cash_return=0.0)

    rows = []
    signal_map = {}
    for name, sig, meta in candidates:
        candidate_cost = 0.0 if name == "buy_hold" else cost
        full = compute_metric(df, sig, cost=candidate_cost, cash_return=cash_return)
        train = compute_segment_metric(df, sig, "2020-01-01", "2021-12-31", cost=cost, cash_return=cash_return)
        valid = compute_segment_metric(df, sig, "2022-01-01", "2023-12-31", cost=cost, cash_return=cash_return)
        test = compute_segment_metric(df, sig, "2024-01-01", None, cost=cost, cash_return=cash_return)
        row = {
            "name": name,
            **meta,
            "cagr": full.cagr,
            "excess_cagr_vs_buyhold": full.cagr - benchmark.cagr,
            "total_return": full.total_return,
            "max_drawdown": full.max_drawdown,
            "annual_vol": full.annual_vol,
            "sharpe": full.sharpe,
            "exposure": full.exposure,
            "turnover": full.turnover,
            "changes": full.changes,
            "turnover_per_year": full.turnover / full.years,
            "changes_per_year": full.changes / full.years,
            "min_position": float(np.nanmin(sig)),
            "max_position": float(np.nanmax(sig)),
            "train_cagr": train.cagr if train else np.nan,
            "train_mdd": train.max_drawdown if train else np.nan,
            "valid_cagr": valid.cagr if valid else np.nan,
            "valid_mdd": valid.max_drawdown if valid else np.nan,
            "test_cagr": test.cagr if test else np.nan,
            "test_mdd": test.max_drawdown if test else np.nan,
        }
        row["target_pass"] = target_pass(row)
        row["objective"] = (
            1.8 * max(row["cagr"] - MIN_TARGET_CAGR, -0.15)
            + 0.7 * row["excess_cagr_vs_buyhold"]
            + 0.6 * (row["test_cagr"] - (benchmark_test.cagr if benchmark_test else 0))
            + 0.3 * (row["valid_cagr"] - (benchmark_valid.cagr if benchmark_valid else 0))
            + 0.15 * (row["max_drawdown"] - benchmark.max_drawdown)
            - 0.002 * row["turnover_per_year"]
            - 0.0005 * row["changes_per_year"]
        )
        rows.append(row)
        signal_map[name] = sig

    results = pd.DataFrame(rows).sort_values(["target_pass", "objective", "cagr"], ascending=False).reset_index(drop=True)
    target_rows = results[results["target_pass"]]
    selected = target_rows.iloc[0] if not target_rows.empty else results.iloc[0]
    selected_signal = signal_map[selected["name"]]
    summary = {
        "target": TARGET,
        "target_name": TARGET_NAME,
        "start_date": str(df["trade_date"].iloc[0]),
        "end_date": str(df["trade_date"].iloc[-1]),
        "candidate_count": int(len(results)),
        "target_pass_count": int(results["target_pass"].sum()),
        "target_met": bool(results["target_pass"].sum() > 0),
        "selected_name": selected["name"],
        "selected": selected.to_dict(),
        "benchmark": benchmark.__dict__,
        "benchmark_valid_2022_2023": benchmark_valid.__dict__ if benchmark_valid else None,
        "benchmark_test_2024": benchmark_test.__dict__ if benchmark_test else None,
        "min_target_cagr": MIN_TARGET_CAGR,
        "max_acceptable_drawdown": MAX_ACCEPTABLE_DRAWDOWN,
        "max_turnover_per_year": MAX_TURNOVER_PER_YEAR,
        "max_changes_per_year": MAX_CHANGES_PER_YEAR,
        "cost": cost,
        "cash_annual_return": cash_return,
        "no_leverage": True,
        "no_short": True,
    }
    return results, summary, selected_signal


def signal_for_strategy(scored: pd.DataFrame, strategy_name: str) -> np.ndarray:
    df = scored.sort_values("trade_date").dropna(subset=["close", "score"]).reset_index(drop=True)
    df["date"] = pd.to_datetime(df["trade_date"])
    df["daily_ret"] = df["close"].pct_change().fillna(0.0)
    for name, signal, _meta in generate_candidates(df):
        if name == strategy_name:
            return signal
    raise RuntimeError(f"Strategy not found in candidate set: {strategy_name}")


def build_nav(scored: pd.DataFrame, signal: np.ndarray, cost: float, cash_return: float) -> pd.DataFrame:
    df = scored.sort_values("trade_date").dropna(subset=["close", "score"]).reset_index(drop=True)
    df["date"] = pd.to_datetime(df["trade_date"])
    df["daily_ret"] = df["close"].pct_change().fillna(0.0)
    df["target_position"] = pd.Series(signal, index=df.index).ffill().fillna(1.0).clip(0, 1)
    df["position"] = df["target_position"].shift(1).fillna(1.0)
    df["turnover"] = (df["position"] - df["position"].shift(1).fillna(df["position"].iloc[0])).abs()
    cash_daily = (1.0 + cash_return) ** (1 / 252) - 1
    df["strategy_ret"] = df["position"] * df["daily_ret"] + (1 - df["position"]) * cash_daily - df["turnover"] * cost
    df["buyhold_ret"] = df["daily_ret"]
    df["strategy_nav"] = (1 + df["strategy_ret"]).cumprod()
    df["buyhold_nav"] = (1 + df["buyhold_ret"]).cumprod()
    return df


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
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return text


def markdown_table(df: pd.DataFrame, columns: list[str]) -> str:
    rows = ["|" + "|".join(columns) + "|", "|" + "|".join(["---"] * len(columns)) + "|"]
    for _, row in df.iterrows():
        rows.append("|" + "|".join(str(row.get(col, "")) for col in columns) + "|")
    return "\n".join(rows)


def format_component_value(metric: str, value: float) -> str:
    if metric in {"近2年价格分位", "RSI14"}:
        return num(value, 2)
    if "涨幅" in metric or "波动" in metric or "回撤" in metric or "均线" in metric or "相对" in metric:
        return pct(value)
    return num(value, 2)


def describe_strategy(selected: dict) -> str:
    freq = {"daily": "每日", "weekly": "每周最后一个交易日", "monthly": "每月最后一个交易日"}.get(
        selected.get("freq", "daily"), selected.get("freq", "daily")
    )
    family = selected.get("family")
    base = float(selected.get("base", 0))
    if family in {"trend_low", "trend_score"}:
        ma = int(selected["ma"])
        low = float(selected.get("low_score", 0))
        high = selected.get("high_score")
        if high is None or not np.isfinite(high):
            return f"{freq}确认：收盘价高于 MA{ma} 或评分 <= {low:.1f} 时满仓，否则仓位 {pct(base)}。"
        return (
            f"{freq}确认：收盘价高于 MA{ma} 且评分 < {float(high):.1f}，或评分 <= {low:.1f} 时满仓；"
            f"否则仓位 {pct(base)}。"
        )
    if family == "band":
        ma = int(selected["ma"])
        low = float(selected["low_score"])
        band = float(selected["band"])
        return (
            f"{freq}确认：收盘价突破 MA{ma} 上方 {pct(band)} 或评分 <= {low:.1f} 时满仓；"
            f"跌破 MA{ma} 下方 {pct(band)} 且评分 > {low:.1f} 时仓位 {pct(base)}；其他情况保持上一目标。"
        )
    if family == "momentum":
        return (
            f"{freq}确认：{int(selected['ret_window'])} 日涨幅高于 {pct(float(selected['threshold']))} "
            f"或评分 <= {float(selected['low_score']):.1f} 时满仓，否则仓位 {pct(base)}。"
        )
    if family == "pure_momentum":
        return (
            f"{freq}确认：{int(selected['ret_window'])} 日涨幅高于 {pct(float(selected['threshold']))} "
            f"时满仓，否则仓位 {pct(base)}。"
        )
    if family == "score_only":
        return f"{freq}确认：评分 < {float(selected['high_score']):.1f} 时满仓，否则仓位 {pct(base)}。"
    return "买入并持有。"


def write_outputs(
    scored: pd.DataFrame,
    components: pd.DataFrame,
    zone_bt: pd.DataFrame,
    grid: pd.DataFrame,
    summary: dict,
    nav: pd.DataFrame,
    cost: float,
    cash_return: float,
    intraday_meta: dict | None = None,
) -> Path:
    ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)
    selected = summary["selected"]
    benchmark = summary["benchmark"]
    latest = scored.dropna(subset=["score"]).iloc[-1]
    target_position = float(nav.iloc[-1]["target_position"])
    if target_position <= 0.005:
        action = f"降至/维持 {pct(target_position)}，未投入资金做现金管理"
    elif target_position >= 0.995:
        action = f"配置/维持 {pct(target_position)} 科创50"
    else:
        action = f"调整到 {pct(target_position)} 科创50"
    data_note = "官方日线"
    if intraday_meta:
        data_note = f"实时行情 {intraday_meta.get('quote_date', '')} {intraday_meta.get('quote_time', '')}".strip()
    execution_note = f"该目标由 {ymd(str(latest['trade_date']))} 信号给出，按下一交易日再平衡执行。"

    scored.to_csv(ANALYSIS_DIR / "kcb50_score_history.csv", index=False, encoding="utf-8-sig")
    components.to_csv(ANALYSIS_DIR / "kcb50_score_components_latest.csv", index=False, encoding="utf-8-sig")
    zone_bt.to_csv(ANALYSIS_DIR / "kcb50_score_zone_backtest.csv", index=False, encoding="utf-8-sig")
    grid.to_csv(ANALYSIS_DIR / "kcb50_strategy_research_grid.csv", index=False, encoding="utf-8-sig")
    nav.to_csv(ANALYSIS_DIR / "kcb50_recommended_strategy_nav.csv", index=False, encoding="utf-8-sig")

    def metric_from_ret(ret: pd.Series) -> dict[str, float]:
        tmp_nav = (1 + ret).cumprod()
        years = years_between(nav["date"])
        cagr = tmp_nav.iloc[-1] ** (1 / years) - 1
        dd = tmp_nav / tmp_nav.cummax() - 1
        return {
            "cagr": float(cagr),
            "total_return": float(tmp_nav.iloc[-1] - 1),
            "max_drawdown": float(dd.min()),
        }

    cost_rows = []
    for test_cost in [0, 0.0005, 0.001, 0.002, 0.003, 0.005]:
        cash_daily = (1.0 + cash_return) ** (1 / 252) - 1
        ret = nav["position"] * nav["buyhold_ret"] + (1 - nav["position"]) * cash_daily - nav["turnover"] * test_cost
        cost_rows.append({"turnover_cost": test_cost, **metric_from_ret(ret)})
    cost_sensitivity = pd.DataFrame(cost_rows)
    cost_sensitivity.to_csv(ANALYSIS_DIR / "kcb50_recommended_strategy_costs.csv", index=False, encoding="utf-8-sig")

    cash_rows = []
    for test_cash in [0, 0.005, 0.01, 0.015, 0.02, 0.025, 0.03]:
        cash_daily = (1.0 + test_cash) ** (1 / 252) - 1
        ret = nav["position"] * nav["buyhold_ret"] + (1 - nav["position"]) * cash_daily - nav["turnover"] * cost
        cash_rows.append({"cash_annual_return": test_cash, **metric_from_ret(ret)})
    cash_sensitivity = pd.DataFrame(cash_rows)
    cash_sensitivity.to_csv(
        ANALYSIS_DIR / "kcb50_recommended_strategy_cash_sensitivity.csv", index=False, encoding="utf-8-sig"
    )
    (ANALYSIS_DIR / "kcb50_strategy_research_summary.json").write_text(
        json.dumps(json_ready(summary), ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8"
    )
    latest_json = {
        "index_code": TARGET,
        "index_name": TARGET_NAME,
        "latest_date": str(latest["trade_date"]),
        "latest_close": float(latest["close"]),
        "score": float(latest["score"]),
        "zone": zone(float(latest["score"])),
        "recommended_strategy": summary["selected_name"],
        "target_position": target_position,
        "action": action,
        "execution_note": execution_note,
        "data_source": "realtime_quote" if intraday_meta else "official_index_daily",
        "quote_time": intraday_meta.get("quote_time") if intraday_meta else None,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    (ANALYSIS_DIR / "kcb50_latest_signal.json").write_text(
        json.dumps(json_ready(latest_json), ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8"
    )

    top = grid.head(20).copy()
    top_cols = [
        "name",
        "family",
        "cagr",
        "excess_cagr_vs_buyhold",
        "max_drawdown",
        "valid_cagr",
        "test_cagr",
        "exposure",
        "turnover_per_year",
        "changes_per_year",
        "target_pass",
    ]
    top_fmt = top[top_cols].rename(
        columns={
            "name": "策略",
            "family": "类型",
            "cagr": "年化",
            "excess_cagr_vs_buyhold": "相对持有",
            "max_drawdown": "最大回撤",
            "valid_cagr": "2022-2023",
            "test_cagr": "2024至今",
            "exposure": "平均仓位",
            "turnover_per_year": "年均换手",
            "changes_per_year": "年均变化",
            "target_pass": "达标",
        }
    )
    for col in ["年化", "相对持有", "最大回撤", "2022-2023", "2024至今", "平均仓位"]:
        top_fmt[col] = top_fmt[col].map(pct)
    top_fmt["年均换手"] = top_fmt["年均换手"].map(lambda x: num(x, 2))
    top_fmt["年均变化"] = top_fmt["年均变化"].map(lambda x: num(x, 2))
    top_fmt["达标"] = top_fmt["达标"].map(lambda x: "是" if bool(x) else "否")

    comp_fmt = components.copy()
    comp_fmt["value_fmt"] = comp_fmt.apply(lambda r: format_component_value(r["metric"], r["value"]), axis=1)
    comp_fmt["score_fmt"] = comp_fmt["score"].map(lambda x: num(x, 2))
    comp_fmt["weight_fmt"] = comp_fmt["weight"].map(pct)
    comp_fmt["contribution_fmt"] = comp_fmt["contribution"].map(lambda x: num(x, 2))
    comp_fmt = comp_fmt.rename(columns={"component": "指标", "metric": "口径"})

    zone_120 = zone_bt[zone_bt["horizon_days"] == 120].copy()
    zone_120["胜率"] = zone_120["win_rate"].map(pct)
    zone_120["平均收益"] = zone_120["avg_return"].map(pct)
    zone_120["中位收益"] = zone_120["median_return"].map(pct)
    zone_120 = zone_120.rename(columns={"zone": "评分区间", "samples": "样本数"})

    cost_fmt = cost_sensitivity.copy()
    cost_fmt["单次成本"] = cost_fmt["turnover_cost"].map(pct)
    cost_fmt["年化"] = cost_fmt["cagr"].map(pct)
    cost_fmt["最大回撤"] = cost_fmt["max_drawdown"].map(pct)
    cost_fmt["累计收益"] = cost_fmt["total_return"].map(pct)

    cash_fmt = cash_sensitivity.copy()
    cash_fmt["现金年化"] = cash_fmt["cash_annual_return"].map(pct)
    cash_fmt["年化"] = cash_fmt["cagr"].map(pct)
    cash_fmt["最大回撤"] = cash_fmt["max_drawdown"].map(pct)
    cash_fmt["累计收益"] = cash_fmt["total_return"].map(pct)

    strategy_rule = describe_strategy(selected)
    report = f"""# 科创50 评分与行动策略研究

生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

## 当前结论

- 指数：{TARGET_NAME}（{TARGET}）
- 最新交易日：{ymd(str(latest['trade_date']))}
- 最新点位：{num(float(latest['close']), 2)}
- 数据口径：{data_note}
- 当前评分：**{num(float(latest['score']), 2)} / 10**，{zone(float(latest['score']))}
- 推荐策略：`{summary['selected_name']}`
- 当前策略目标仓位：**{pct(target_position)}**
- 当前提示：**{action}**
- 执行口径：{execution_note}

## 评分方法

分数越低代表相对低温、回撤较深或不拥挤；分数越高代表相对高温、动量拥挤或波动风险更高。每个指标使用截至当日可见的扩展历史分位，至少 {MIN_SCORE_OBSERVATIONS} 个有效样本后才出分。

{markdown_table(comp_fmt, ['指标', '口径', 'value_fmt', 'score_fmt', 'weight_fmt', 'contribution_fmt'])}

## 策略筛选口径

- 只做多科创50，不使用杠杆，不做空；仓位限制 0%-100%。
- 信号收盘后确认，下一交易日执行。
- 仓位变化成本：{pct(cost)}。
- 未投入指数的资金按现金管理年化 {pct(cash_return)} 计入。
- 目标：策略全样本年化收益不低于 {pct(MIN_TARGET_CAGR)}，最大回撤不差于 {pct(MAX_ACCEPTABLE_DRAWDOWN)}，年均换手不超过 {num(MAX_TURNOVER_PER_YEAR, 1)} 倍，年均仓位变化不超过 {num(MAX_CHANGES_PER_YEAR, 1)} 次，且 2022-2023 与 2024 至今收益为正。
- 候选策略：{summary['candidate_count']} 个；达标：{summary['target_pass_count']} 个。

## 推荐行动策略

- 规则：{strategy_rule}
- 策略年化收益：{pct(float(selected['cagr']))}
- 买入持有年化收益：{pct(float(benchmark['cagr']))}
- 相对持有年化超额：{pct(float(selected['excess_cagr_vs_buyhold']))}
- 策略累计收益：{pct(float(selected['total_return']))}
- 买入持有累计收益：{pct(float(benchmark['total_return']))}
- 策略最大回撤：{pct(float(selected['max_drawdown']))}
- 买入持有最大回撤：{pct(float(benchmark['max_drawdown']))}
- 策略年化波动：{pct(float(selected['annual_vol']))}
- 平均仓位：{pct(float(selected['exposure']))}
- 年均换手：{num(float(selected['turnover_per_year']), 2)} 倍
- 年均仓位变化：{num(float(selected['changes_per_year']), 2)} 次
- 2022-2023 年化：{pct(float(selected['valid_cagr']))}
- 2024 至今年化：{pct(float(selected['test_cagr']))}

## 综合排名前 20

{markdown_table(top_fmt, list(top_fmt.columns))}

## 评分区间历史表现

未来 120 个交易日：

{markdown_table(zone_120, ['评分区间', '样本数', '胜率', '平均收益', '中位收益'])}

## 敏感性

成本敏感性：

{markdown_table(cost_fmt, ['单次成本', '年化', '最大回撤', '累计收益'])}

现金收益敏感性：

{markdown_table(cash_fmt, ['现金年化', '年化', '最大回撤', '累计收益'])}

## 使用提醒

科创50历史样本从 2019 年底开始，回测区间明显短于红利低波，达标策略更依赖 2021 年后大回撤期间的风险规避效果。该策略在 0.10% 成本、空仓现金年化 2.00% 口径下刚超过 15%；若空仓现金收益为 0 或交易成本明显更高，则不再达标。该报告适合作为仓位管理研究，不应理解为收益保证。
"""
    report_path = ANALYSIS_DIR / "kcb50_strategy_report.md"
    report_path.write_text(report, encoding="utf-8")
    return report_path


def json_ready(value):
    if isinstance(value, dict):
        return {str(k): json_ready(v) for k, v in value.items()}
    if isinstance(value, list):
        return [json_ready(v) for v in value]
    if isinstance(value, (np.bool_, bool)):
        return bool(value)
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        if not np.isfinite(value):
            return None
        return float(value)
    return value


def main() -> int:
    args = parse_args()
    pro = init_tushare()
    target = fetch_index_daily(pro, TARGET, args.start_date, args.end_date, args.refresh)
    benchmarks = {}
    for code in BENCHMARKS:
        time.sleep(0.2)
        benchmarks[code] = fetch_index_daily(pro, code, args.start_date, args.end_date, args.refresh)

    official_indicators = build_indicator_frame(target, benchmarks)
    official_scored, _official_components = add_score(official_indicators)
    official_scored = add_forward_returns(official_scored, [60, 120, 250])
    grid, summary, selected_signal = research_strategies(official_scored, args.cost, args.cash_return)

    intraday_meta = None
    if args.intraday:
        target, benchmarks, intraday_meta = apply_intraday_quotes(target, benchmarks)

    indicators = build_indicator_frame(target, benchmarks)
    scored, components = add_score(indicators)
    scored = add_forward_returns(scored, [60, 120, 250])
    zone_bt = zone_backtest(scored, [60, 120, 250])
    if args.intraday:
        selected_signal = signal_for_strategy(scored, summary["selected_name"])
    nav = build_nav(scored, selected_signal, args.cost, args.cash_return)
    report_path = write_outputs(scored, components, zone_bt, grid, summary, nav, args.cost, args.cash_return, intraday_meta)

    selected = summary["selected"]
    latest = scored.dropna(subset=["score"]).iloc[-1]
    print(f"Report: {report_path}")
    print(f"Latest date: {latest['trade_date']}; score={latest['score']:.2f}/10")
    print(f"Candidates: {summary['candidate_count']}; target pass: {summary['target_pass_count']}")
    print(
        f"Selected: {summary['selected_name']}; CAGR={selected['cagr']:.4%}; "
        f"buyhold={summary['benchmark']['cagr']:.4%}; MDD={selected['max_drawdown']:.4%}"
    )
    print(f"Target position: {nav.iloc[-1]['target_position']:.2%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
