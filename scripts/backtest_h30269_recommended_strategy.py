#!/usr/bin/env python3
"""Recommended H30269 action strategy.

Main rule (monthly signal, next-trading-day execution):
- At each month-end close, hold 100% H30269 if score <= 4.0.
- Otherwise hold 100% H30269 if close > MA24.
- Otherwise hold 0% H30269 and keep the capital in cash-like instruments.

The backtest uses as-of expanding score percentiles to avoid look-ahead bias.
Signals (MA24, score) stay on the price index H30269.CSI; strategy and
buy-and-hold returns are computed on the total-return index h20269.CSI so the
benchmark keeps its dividends (the price index understates holding returns by
~4%/year and would overstate the value of switching to cash).
"""

from __future__ import annotations

import bisect
import json
import math
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd


BASE_DIR = Path(__file__).resolve().parents[1]
ANALYSIS_DIR = BASE_DIR / "analysis" / "h30269"
SCORE_HISTORY = ANALYSIS_DIR / "h30269_score_history.csv"

BASE_POSITION = 0.0
MA_WINDOW = 24
LOW_SCORE = 4.0
TURNOVER_COST = 0.001
CASH_ANNUAL_RETURN = 0.02
MIN_SCORE_OBSERVATIONS = 252

COMPONENTS = [
    ("price_pct_3y", 0.18),
    ("dd_3y", 0.12),
    ("ma250_dev", 0.12),
    ("rsi14", 0.10),
    ("ret_60", 0.10),
    ("ret_250", 0.10),
    ("vol_60", 0.10),
    ("rel_250_000300_SH", 0.09),
    ("rel_250_000922_CSI", 0.09),
]


def pct(value: float | None) -> str:
    if value is None or not np.isfinite(value):
        return "-"
    return f"{value * 100:.2f}%"


def num(value: float | None, digits: int = 2) -> str:
    if value is None or not np.isfinite(value):
        return "-"
    return f"{value:.{digits}f}"


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


def add_asof_score(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    total = pd.Series(0.0, index=out.index)
    valid = pd.Series(True, index=out.index)
    for key, weight in COMPONENTS:
        component_score = expanding_percentile(out[key]) * 10
        total = total + component_score.fillna(0) * weight
        valid &= component_score.notna()
    out["score_reported"] = out.get("score")
    out["score"] = total.where(valid)
    return out


def reconstruct_total_return(df: pd.DataFrame) -> pd.Series:
    """全收益收盘序列；缺口（盘中估算日、全收益当日未发布）用价格收益顺延填补."""
    if "tr_close" not in df.columns:
        raise SystemExit(
            "score history lacks tr_close; rerun analyze_h30269.py (it fetches h20269.CSI) first"
        )
    price = pd.to_numeric(df["close"], errors="coerce").to_numpy(dtype=float)
    tr = pd.to_numeric(df["tr_close"], errors="coerce").to_numpy(dtype=float)
    out = np.full(len(df), np.nan)
    for i in range(len(df)):
        if np.isfinite(tr[i]):
            out[i] = tr[i]
        elif i > 0 and np.isfinite(out[i - 1]) and price[i - 1] > 0 and np.isfinite(price[i]):
            out[i] = out[i - 1] * (price[i] / price[i - 1])
    return pd.Series(out, index=df.index)


def metric(frame: pd.DataFrame, ret_col: str, nav_col: str) -> dict[str, float]:
    nav = frame[nav_col] / frame[nav_col].iloc[0]
    ret = frame[ret_col]
    years = (frame["date"].iloc[-1] - frame["date"].iloc[0]).days / 365.25
    cagr = nav.iloc[-1] ** (1 / years) - 1
    mdd = (nav / nav.cummax() - 1).min()
    vol = ret.std() * math.sqrt(252)
    return {
        "years": float(years),
        "total_return": float(nav.iloc[-1] - 1),
        "cagr": float(cagr),
        "max_drawdown": float(mdd),
        "annual_vol": float(vol),
        "sharpe_0rf": float(cagr / vol) if vol else np.nan,
    }


def segment(df: pd.DataFrame, start: str, end: str | None = None) -> dict[str, float | str]:
    tmp = df[df["date"] >= pd.Timestamp(start)].copy()
    if end:
        tmp = tmp[tmp["date"] <= pd.Timestamp(end)]
    tmp = tmp.reset_index(drop=True)
    sm = metric(tmp, "strategy_ret", "strategy_nav")
    bm = metric(tmp, "buyhold_ret", "buyhold_nav")
    return {
        "start": tmp["trade_date"].iloc[0],
        "end": tmp["trade_date"].iloc[-1],
        "strategy_cagr": sm["cagr"],
        "strategy_mdd": sm["max_drawdown"],
        "buyhold_cagr": bm["cagr"],
        "buyhold_mdd": bm["max_drawdown"],
        "strategy_exposure": float(tmp["position"].mean()),
        "changes": int((tmp["turnover"] > 0).sum()),
    }


def month_end_indices(df: pd.DataFrame) -> pd.Index:
    keys = df["date"].dt.to_period("M")
    month_ends = pd.DataFrame({"key": keys}, index=df.index).groupby("key").tail(1).index
    latest = df["date"].iloc[-1]
    latest_is_period_end = bool(latest.is_month_end or pd.offsets.BMonthEnd().is_on_offset(latest))
    if len(month_ends) and month_ends[-1] == df.index[-1] and not latest_is_period_end:
        month_ends = month_ends[:-1]
    return month_ends


def build_signal(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["trend_trigger"] = out["close"] > out[f"ma{MA_WINDOW}"]
    out["low_score_trigger"] = out["score"] <= LOW_SCORE
    out["raw_target_position"] = np.where(
        out["low_score_trigger"] | out["trend_trigger"],
        1.0,
        BASE_POSITION,
    )
    rebal_idx = month_end_indices(out)
    out["rebalance_due"] = False
    out.loc[rebal_idx, "rebalance_due"] = True
    out["target_position"] = np.nan
    out.loc[rebal_idx, "target_position"] = out.loc[rebal_idx, "raw_target_position"]
    out["target_position"] = out["target_position"].ffill().fillna(1.0)
    out["position"] = out["target_position"].shift(1).fillna(1.0)
    return out


def main() -> int:
    raw_df = pd.read_csv(SCORE_HISTORY, dtype={"trade_date": str}).sort_values("trade_date").reset_index(drop=True)
    raw_df["tr_nav_close"] = reconstruct_total_return(raw_df)
    raw_price = raw_df.dropna(subset=["close", "tr_nav_close"]).reset_index(drop=True)
    raw_price["date"] = pd.to_datetime(raw_price["trade_date"])
    raw_price["buyhold_ret"] = raw_price["tr_nav_close"].pct_change().fillna(0)
    raw_price["buyhold_nav"] = (1 + raw_price["buyhold_ret"]).cumprod()
    inception_buyhold = metric(raw_price, "buyhold_ret", "buyhold_nav")

    df = add_asof_score(raw_df)
    df = df.dropna(subset=["score", "close"]).reset_index(drop=True)
    df["date"] = pd.to_datetime(df["trade_date"])
    # 收益按全收益指数；点位、MA 与评分仍按价格指数
    df["daily_ret"] = df["tr_nav_close"].pct_change().fillna(0)
    df[f"ma{MA_WINDOW}"] = df["close"].rolling(MA_WINDOW, min_periods=MA_WINDOW).mean()
    df = build_signal(df)

    cash_daily = (1.0 + CASH_ANNUAL_RETURN) ** (1 / 252) - 1
    df["turnover"] = (df["position"] - df["position"].shift(1).fillna(df["position"].iloc[0])).abs()
    df["strategy_ret"] = (
        df["position"] * df["daily_ret"] + (1 - df["position"]) * cash_daily - df["turnover"] * TURNOVER_COST
    )
    df["buyhold_ret"] = df["daily_ret"]
    df["strategy_nav"] = (1 + df["strategy_ret"]).cumprod()
    df["buyhold_nav"] = (1 + df["buyhold_ret"]).cumprod()

    full_strategy = metric(df, "strategy_ret", "strategy_nav")
    full_buyhold = metric(df, "buyhold_ret", "buyhold_nav")
    segments = pd.DataFrame(
        [
            {"segment": "2008-2012", **segment(df, "2008-01-01", "2012-12-31")},
            {"segment": "2013-2018", **segment(df, "2013-01-01", "2018-12-31")},
            {"segment": "2016-2020", **segment(df, "2016-01-01", "2020-12-31")},
            {"segment": "2019-2026", **segment(df, "2019-01-01")},
            {"segment": "2021-2026", **segment(df, "2021-01-01")},
        ]
    )

    costs = []
    for cost in [0, 0.0005, 0.001, 0.002, 0.003, 0.005]:
        ret = df["position"] * df["daily_ret"] + (1 - df["position"]) * cash_daily - df["turnover"] * cost
        nav = (1 + ret).cumprod()
        tmp = df.copy()
        tmp["tmp_ret"] = ret
        tmp["tmp_nav"] = nav
        costs.append({"turnover_cost": cost, **metric(tmp, "tmp_ret", "tmp_nav")})
    costs = pd.DataFrame(costs)

    cash_sensitivity = []
    for cash_return in [0, 0.005, 0.01, 0.015, 0.02, 0.025, 0.03]:
        daily = (1.0 + cash_return) ** (1 / 252) - 1
        ret = df["position"] * df["daily_ret"] + (1 - df["position"]) * daily - df["turnover"] * TURNOVER_COST
        nav = (1 + ret).cumprod()
        tmp = df.copy()
        tmp["tmp_ret"] = ret
        tmp["tmp_nav"] = nav
        cash_sensitivity.append({"cash_annual_return": cash_return, **metric(tmp, "tmp_ret", "tmp_nav")})
    cash_sensitivity = pd.DataFrame(cash_sensitivity)

    latest = df.iloc[-1]
    ma_col = f"ma{MA_WINDOW}"
    signal = {
        "trade_date": latest["trade_date"],
        "close": float(latest["close"]),
        "score": float(latest["score"]),
        "reported_score": float(latest["score_reported"]) if pd.notna(latest.get("score_reported")) else None,
        "ma_window": MA_WINDOW,
        "ma": float(latest[ma_col]),
        "target_position": float(latest["target_position"]),
        "applied_position": float(latest["position"]),
        "raw_target_if_rebalanced_today": float(latest["raw_target_position"]),
        "low_score_trigger": bool(latest["low_score_trigger"]),
        "trend_trigger": bool(latest["trend_trigger"]),
        "cash_exit_trigger": bool(
            (not latest["low_score_trigger"]) and (not latest["trend_trigger"]) and latest["raw_target_position"] <= 0.001
        ),
        "rebalance_due": bool(latest["rebalance_due"]),
        "strategy_type": "monthly_core_trend_low",
        "rule": (
            f"Month-end 100% if score <= {LOW_SCORE} or close > MA{MA_WINDOW}; "
            f"otherwise {BASE_POSITION:.0%} H30269 and cash-like instruments"
        ),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    summary = {
        "rule_name": "H30269 monthly MA24 low-score strategy",
        "strategy_type": "monthly_core_trend_low",
        "return_basis": "total_return_h20269",
        "base_position": BASE_POSITION,
        "ma_window": MA_WINDOW,
        "low_score": LOW_SCORE,
        "turnover_cost": TURNOVER_COST,
        "cash_annual_return": CASH_ANNUAL_RETURN,
        "min_score_observations": MIN_SCORE_OBSERVATIONS,
        "start_date": df["trade_date"].iloc[0],
        "end_date": df["trade_date"].iloc[-1],
        "strategy": full_strategy,
        "buyhold": full_buyhold,
        "excess_cagr_vs_buyhold": full_strategy["cagr"] - full_buyhold["cagr"],
        "inception_buyhold": inception_buyhold,
        "inception_start_date": raw_price["trade_date"].iloc[0],
        "avg_exposure": float(df["position"].mean()),
        "position_changes": int((df["turnover"] > 0).sum()),
        "avg_changes_per_year": int((df["turnover"] > 0).sum()) / full_strategy["years"],
        "total_turnover": float(df["turnover"].sum()),
        "avg_turnover_per_year": float(df["turnover"].sum()) / full_strategy["years"],
        "current_signal": signal,
    }

    out_cols = [
        "trade_date",
        "close",
        "tr_nav_close",
        "score",
        "score_reported",
        ma_col,
        "trend_trigger",
        "low_score_trigger",
        "rebalance_due",
        "raw_target_position",
        "target_position",
        "position",
        "turnover",
        "strategy_ret",
        "strategy_nav",
        "buyhold_nav",
    ]
    df[out_cols].to_csv(ANALYSIS_DIR / "h30269_recommended_strategy_nav.csv", index=False, encoding="utf-8-sig")
    segments.to_csv(ANALYSIS_DIR / "h30269_recommended_strategy_segments.csv", index=False, encoding="utf-8-sig")
    costs.to_csv(ANALYSIS_DIR / "h30269_recommended_strategy_costs.csv", index=False, encoding="utf-8-sig")
    cash_sensitivity.to_csv(ANALYSIS_DIR / "h30269_recommended_strategy_cash_sensitivity.csv", index=False, encoding="utf-8-sig")
    (ANALYSIS_DIR / "h30269_recommended_strategy_summary.json").write_text(
        json.dumps(json_ready(summary), ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8"
    )
    (ANALYSIS_DIR / "h30269_recommended_strategy_signal.json").write_text(
        json.dumps(json_ready(signal), ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8"
    )

    segment_lines = []
    for _, row in segments.iterrows():
        segment_lines.append(
            f"|{row['segment']}|{pct(row['strategy_cagr'])}|{pct(row['buyhold_cagr'])}|"
            f"{pct(row['strategy_mdd'])}|{pct(row['buyhold_mdd'])}|{pct(row['strategy_exposure'])}|{int(row['changes'])}|"
        )
    cost_lines = []
    for _, row in costs.iterrows():
        cost_lines.append(
            f"|{pct(row['turnover_cost'])}|{pct(row['cagr'])}|{pct(row['max_drawdown'])}|{pct(row['total_return'])}|"
        )
    cash_lines = []
    for _, row in cash_sensitivity.iterrows():
        cash_lines.append(
            f"|{pct(row['cash_annual_return'])}|{pct(row['cagr'])}|{pct(row['cagr'] - full_buyhold['cagr'])}|"
            f"{pct(row['max_drawdown'])}|"
        )

    report = f"""# H30269 推荐策略回测

生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

## 推荐规则

- 每月最后一个交易日收盘后确认，下一交易日执行
- 评分 <= {LOW_SCORE}：目标仓位 100%
- 否则，收盘价 > MA{MA_WINDOW}：目标仓位 100%
- 否则：目标仓位 {pct(BASE_POSITION)}，未投入 H30269 的资金进入现金管理

回测扣除 {pct(TURNOVER_COST)} 的仓位变动成本，并假设空仓资金年化 {pct(CASH_ANNUAL_RETURN)}。评分历史使用截至当日可见的扩展分位，避免使用未来样本。策略持仓与"持有不动"收益均按红利低波**全收益指数 h20269.CSI**（含股息再投资）计算；点位、MA{MA_WINDOW} 与评分仍按价格指数 H30269.CSI 口径。

## 全样本结果

- 样本区间：{summary['start_date']} 至 {summary['end_date']}，约 {full_strategy['years']:.2f} 年
- 策略年化收益：{pct(full_strategy['cagr'])}
- 同区间持有不动年化收益：{pct(full_buyhold['cagr'])}
- 年化超额：{pct(summary['excess_cagr_vs_buyhold'])}
- 策略累计收益：{pct(full_strategy['total_return'])}
- 同区间持有不动累计收益：{pct(full_buyhold['total_return'])}
- 策略最大回撤：{pct(full_strategy['max_drawdown'])}
- 同区间持有不动最大回撤：{pct(full_buyhold['max_drawdown'])}
- 策略年化波动：{pct(full_strategy['annual_vol'])}
- 同区间持有不动年化波动：{pct(full_buyhold['annual_vol'])}
- 平均仓位：{pct(summary['avg_exposure'])}
- 仓位变化：{summary['position_changes']} 次，约 {summary['avg_changes_per_year']:.1f} 次/年
- 年均换手：{summary['avg_turnover_per_year']:.1f} 倍目标资金

说明：策略需要无未来函数评分，所以策略比较从 {summary['start_date']} 开始；全收益指数最早可取日为 {summary['inception_start_date']}，若从该日直接持有全收益指数到 {summary['end_date']}，年化收益为 {pct(inception_buyhold['cagr'])}，累计收益为 {pct(inception_buyhold['total_return'])}。

## 分段检验

|区间|策略年化|持有年化|策略最大回撤|持有最大回撤|平均仓位|仓位变化|
|---|---:|---:|---:|---:|---:|---:|
{chr(10).join(segment_lines)}

## 成本敏感性

|单次仓位变动成本|策略年化|最大回撤|累计收益|
|---:|---:|---:|---:|
{chr(10).join(cost_lines)}

## 现金收益敏感性

|空仓现金年化|策略年化|年化超额|最大回撤|
|---:|---:|---:|---:|
{chr(10).join(cash_lines)}

## 当前信号

- 最新交易日：{signal['trade_date']}
- 收盘点位：{num(signal['close'], 2)}
- 当前评分：{num(signal['score'], 2)}
- MA{MA_WINDOW}：{num(signal['ma'], 2)}
- 是否月度确认日：{'是' if signal['rebalance_due'] else '否'}
- 是否低分保护：{'是' if signal['low_score_trigger'] else '否'}
- 是否站上 MA{MA_WINDOW}：{'是' if signal['trend_trigger'] else '否'}
- 如果今天确认，目标仓位：{pct(signal['raw_target_if_rebalanced_today'])}
- 下一交易日策略目标仓位：{pct(signal['target_position'])}

## 结论

该规则不是预测涨跌，而是月度仓位管理：低分或短期趋势恢复时持有红利低波；趋势弱且评分不低时退到现金管理。现金管理假设是回测口径的一部分，执行时需要真实落实。全收益口径下，该策略的长期超额主要来自 2008-2016 的极端行情保护，2019 年以来的主要贡献是降低回撤而非增厚收益（详见 analysis/h30269/robust_research/）。
"""
    (ANALYSIS_DIR / "h30269_recommended_strategy_report.md").write_text(report, encoding="utf-8")
    print(report)
    return 0


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


if __name__ == "__main__":
    raise SystemExit(main())
