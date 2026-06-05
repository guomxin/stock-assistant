#!/usr/bin/env python3
"""Apply an intraday H30269 estimate from realtime constituent quotes."""

from __future__ import annotations

import json
import math
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import tushare as ts
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parents[1]
ANALYSIS_DIR = BASE_DIR / "analysis" / "h30269"
TARGET = "H30269.CSI"
BENCHMARKS = ["000300.SH", "000922.CSI"]
COMPONENTS = [
    ("price_pct_3y", "三年价格位置", 0.18, "近3年价格分位"),
    ("dd_3y", "三年回撤位置", 0.12, "距3年高点回撤"),
    ("ma250_dev", "年线偏离", 0.12, "相对250日均线"),
    ("rsi14", "短期拥挤度", 0.10, "RSI14"),
    ("ret_60", "三个月涨幅", 0.10, "60日涨幅"),
    ("ret_250", "一年涨幅", 0.10, "250日涨幅"),
    ("vol_60", "波动风险", 0.10, "60日年化波动"),
    ("rel_250_000300_SH", "相对沪深300热度", 0.09, "一年相对沪深300"),
    ("rel_250_000922_CSI", "相对中证红利热度", 0.09, "一年相对中证红利"),
]


@dataclass(frozen=True)
class IntradayEstimate:
    trade_date: str
    quote_time: str
    weighted_return: float
    close: float
    coverage_weight: float
    quoted_count: int
    total_count: int


def init_tushare():
    load_dotenv(BASE_DIR / ".env")
    token = os.getenv("TUSHARE_TOKEN")
    if not token:
        raise SystemExit("Missing TUSHARE_TOKEN")
    ts.set_token(token)
    return ts.pro_api(token)


def to_float(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def realtime_quote(codes: list[str]) -> pd.DataFrame:
    frames = []
    # Tushare realtime_quote accepts comma-separated A-share codes. Keep batches
    # modest so one odd response cannot sink the whole request.
    for start in range(0, len(codes), 30):
        batch = codes[start : start + 30]
        df = ts.realtime_quote(ts_code=",".join(batch))
        if df is not None and not df.empty:
            frames.append(df)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def estimate_h30269(now: datetime) -> IntradayEstimate:
    constituents = pd.read_csv(ANALYSIS_DIR / "h30269_constituents_latest.csv")
    history = pd.read_csv(ANALYSIS_DIR / "h30269_score_history.csv", dtype={"trade_date": str})
    previous = history.sort_values("trade_date").iloc[-1]
    previous_close = float(previous["close"])

    codes = constituents["con_code"].astype(str).dropna().unique().tolist()
    quotes = realtime_quote(codes)
    if quotes.empty:
        raise RuntimeError("No realtime constituent quotes returned.")

    quotes.columns = [str(col).upper() for col in quotes.columns]
    keep = quotes[["TS_CODE", "PRICE", "PRE_CLOSE", "DATE", "TIME"]].copy()
    for column in ["PRICE", "PRE_CLOSE"]:
        keep[column] = pd.to_numeric(keep[column], errors="coerce")
    keep = keep[(keep["PRICE"] > 0) & (keep["PRE_CLOSE"] > 0)].copy()
    keep["intraday_ret"] = keep["PRICE"] / keep["PRE_CLOSE"] - 1

    merged = constituents.merge(keep, left_on="con_code", right_on="TS_CODE", how="left")
    merged["weight_norm"] = merged["weight"] / merged["weight"].sum()
    valid = merged.dropna(subset=["intraday_ret", "weight_norm"]).copy()
    if valid.empty:
        raise RuntimeError("No valid realtime constituent returns after merge.")

    coverage_weight = float(valid["weight"].sum())
    weighted_return = float(
        (valid["intraday_ret"] * valid["weight_norm"]).sum()
        / valid["weight_norm"].sum()
    )
    trade_date = str(valid["DATE"].dropna().astype(str).max())
    quote_time = str(valid["TIME"].dropna().astype(str).max())
    if not trade_date or trade_date == "nan":
        trade_date = now.strftime("%Y%m%d")
    if not quote_time or quote_time == "nan":
        quote_time = now.strftime("%H:%M:%S")

    estimate = IntradayEstimate(
        trade_date=trade_date,
        quote_time=quote_time,
        weighted_return=weighted_return,
        close=previous_close * (1 + weighted_return),
        coverage_weight=coverage_weight,
        quoted_count=int(valid["con_code"].nunique()),
        total_count=int(constituents["con_code"].nunique()),
    )
    merged.to_csv(ANALYSIS_DIR / "h30269_intraday_constituent_quotes.csv", index=False, encoding="utf-8-sig")
    return estimate


def estimate_benchmark_close(code: str, previous_close: float) -> float:
    df = ts.realtime_quote(ts_code=code)
    if df is None or df.empty:
        return previous_close
    row = df.iloc[0]
    price = to_float(row.get("PRICE"))
    return price if price and price > 0 else previous_close


def rolling_percentile_last(series: pd.Series, window: int) -> pd.Series:
    def calc(values: np.ndarray) -> float:
        current = values[-1]
        if not np.isfinite(current):
            return np.nan
        valid = values[np.isfinite(values)]
        if len(valid) == 0:
            return np.nan
        return float((valid <= current).sum() / len(valid))

    return series.rolling(window, min_periods=max(60, window // 3)).apply(calc, raw=True)


def rsi(series: pd.Series, window: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / window, adjust=False, min_periods=window).mean()
    avg_loss = loss.ewm(alpha=1 / window, adjust=False, min_periods=window).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def recompute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    out = df.sort_values("trade_date").reset_index(drop=True).copy()
    out["ret_1"] = out["close"].pct_change()
    out["pct_chg"] = out["ret_1"] * 100
    out["ma250"] = out["close"].rolling(250, min_periods=120).mean()
    out["ma250_dev"] = out["close"] / out["ma250"] - 1
    out["rolling_high_3y"] = out["close"].rolling(750, min_periods=250).max()
    out["dd_3y"] = out["close"] / out["rolling_high_3y"] - 1
    out["price_pct_3y"] = rolling_percentile_last(out["close"], 750)
    out["rsi14"] = rsi(out["close"])

    for days in [20, 60, 120, 250]:
        out[f"ret_{days}"] = out["close"] / out["close"].shift(days) - 1

    for days in [20, 60, 250]:
        out[f"vol_{days}"] = (
            out["ret_1"].rolling(days, min_periods=max(10, days // 2)).std()
            * math.sqrt(252)
        )

    for code in BENCHMARKS:
        key = code.replace(".", "_")
        bench_ret = out[f"close_{key}"] / out[f"close_{key}"].shift(250) - 1
        out[f"rel_250_{key}"] = out["ret_250"] - bench_ret
    return out


def add_score(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    out = df.copy()
    rows = []
    for key, label, weight, value_label in COMPONENTS:
        score_col = f"{key}_score"
        contribution_col = f"{key}_contribution"
        out[score_col] = out[key].rank(pct=True) * 10
        out[contribution_col] = out[score_col] * weight

    contribution_cols = [f"{key}_contribution" for key, *_ in COMPONENTS]
    out["score"] = out[contribution_cols].sum(axis=1, min_count=len(COMPONENTS))
    latest = out.dropna(subset=["score"]).iloc[-1]
    for key, label, weight, value_label in COMPONENTS:
        rows.append(
            {
                "component": label,
                "metric": value_label,
                "value": latest[key],
                "score": latest[f"{key}_score"],
                "weight": weight,
                "contribution": latest[f"{key}_contribution"],
            }
        )
    return out, pd.DataFrame(rows)


def score_zone(score: float) -> str:
    if score <= 3:
        return "<=3 买入区"
    if score >= 7:
        return ">=7 卖出区"
    return "3-7 中性区"


def write_intraday_score_report(scored: pd.DataFrame, components: pd.DataFrame, meta: dict) -> None:
    latest = scored.dropna(subset=["score"]).iloc[-1]
    lines = [
        "# 红利低波 H30269 盘中评分报告",
        "",
        f"生成时间：{meta['generated_at']}",
        "",
        "## 当前结论",
        "",
        f"- 数据口径：{meta['data_source']}",
        f"- 盘中时间：{meta['quote_date']} {meta['quote_time']}",
        f"- 成分股实时覆盖：{meta['quoted_count']} / {meta['total_count']}，权重覆盖 {meta['coverage_weight']:.2f}%",
        f"- 估算点位：{latest['close']:.2f}",
        f"- 盘中估算涨跌：{meta['intraday_return'] * 100:.2f}%",
        f"- 当前评分：**{latest['score']:.2f} / 10**",
        f"- 当前区间：**{score_zone(float(latest['score']))}**",
        "",
        "## 指标贡献",
        "",
        "|指标|口径|当前值|子分|权重|贡献|",
        "|---|---|---:|---:|---:|---:|",
    ]
    for _, row in components.iterrows():
        value = row["value"]
        lines.append(
            f"|{row['component']}|{row['metric']}|{value:.4f}|"
            f"{row['score']:.2f}|{row['weight'] * 100:.0f}%|{row['contribution']:.2f}|"
        )
    lines.append("")
    lines.append("说明：H30269 本身没有可用的 Tushare 实时指数点位，盘中点位由最新成分股权重和成分股实时涨跌幅估算。")
    text = "\n".join(lines)
    (ANALYSIS_DIR / "h30269_score_report_latest.md").write_text(text, encoding="utf-8")
    (ANALYSIS_DIR / f"h30269_score_report_{meta['quote_date']}_intraday.md").write_text(text, encoding="utf-8")


def main() -> int:
    now = datetime.now()
    init_tushare()
    estimate = estimate_h30269(now)
    history_path = ANALYSIS_DIR / "h30269_score_history.csv"
    history = pd.read_csv(history_path, dtype={"trade_date": str})
    history = history.sort_values("trade_date").reset_index(drop=True)
    previous = history.iloc[-1]

    row = previous.copy()
    row["trade_date"] = estimate.trade_date
    row["close"] = estimate.close
    for code in BENCHMARKS:
        key = code.replace(".", "_")
        prev_col = f"close_{key}"
        row[prev_col] = estimate_benchmark_close(code, float(previous[prev_col]))

    base = history[history["trade_date"] != estimate.trade_date].copy()
    updated = pd.concat([base, pd.DataFrame([row])], ignore_index=True)
    updated = recompute_indicators(updated)
    scored, components = add_score(updated)
    for horizon in [60, 120, 250]:
        scored[f"fwd_{horizon}"] = scored["close"].shift(-horizon) / scored["close"] - 1

    meta = {
        "index_code": TARGET,
        "latest_date": estimate.trade_date,
        "latest_close": float(scored.dropna(subset=["score"]).iloc[-1]["close"]),
        "score": float(scored.dropna(subset=["score"]).iloc[-1]["score"]),
        "zone": score_zone(float(scored.dropna(subset=["score"]).iloc[-1]["score"])),
        "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "data_source": "intraday_constituent_estimate",
        "quote_date": estimate.trade_date,
        "quote_time": estimate.quote_time,
        "intraday_return": estimate.weighted_return,
        "coverage_weight": estimate.coverage_weight,
        "quoted_count": estimate.quoted_count,
        "total_count": estimate.total_count,
    }

    scored.to_csv(history_path, index=False, encoding="utf-8-sig")
    components.to_csv(ANALYSIS_DIR / "h30269_score_components_latest.csv", index=False, encoding="utf-8-sig")
    (ANALYSIS_DIR / "h30269_latest_score.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    write_intraday_score_report(scored, components, meta)
    print(
        "Intraday H30269 estimate "
        f"{estimate.trade_date} {estimate.quote_time}: "
        f"close={estimate.close:.2f}, ret={estimate.weighted_return * 100:.2f}%, "
        f"score={meta['score']:.2f}, coverage={estimate.coverage_weight:.2f}%"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
