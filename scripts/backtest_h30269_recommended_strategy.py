#!/usr/bin/env python3
"""Recommended H30269 strategy after systematic research.

Main rule (daily signal, next-day execution):
- If score <= 4.0, target position is 100% (cheap-score protection).
- Else, use a 15-day moving-average band:
  - close > MA15 * 1.03: target position is 100%.
  - close < MA15 * 0.97 and score > 4.0: target position is 30%.
  - otherwise keep the previous target position.

This is designed to avoid whipsaw while keeping exposure to the long-run
positive carry of the dividend low-volatility index.
"""

from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd


BASE_DIR = Path(__file__).resolve().parents[1]
ANALYSIS_DIR = BASE_DIR / "analysis" / "h30269"
SCORE_HISTORY = ANALYSIS_DIR / "h30269_score_history.csv"

BASE_POSITION = 0.30
MA_WINDOW = 15
BAND = 0.03
LOW_SCORE = 4.0
TURNOVER_COST = 0.001


def pct(value: float | None) -> str:
    if value is None or not np.isfinite(value):
        return "-"
    return f"{value * 100:.2f}%"


def num(value: float | None, digits: int = 2) -> str:
    if value is None or not np.isfinite(value):
        return "-"
    return f"{value:.{digits}f}"


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


def build_signal(df: pd.DataFrame) -> pd.Series:
    enter_full = (df["score"] <= LOW_SCORE) | (df["close"] > df["ma15"] * (1 + BAND))
    exit_to_base = (df["score"] > LOW_SCORE) & (df["close"] < df["ma15"] * (1 - BAND))

    target = []
    state = 1.0
    for enter, exit_ in zip(enter_full.fillna(False), exit_to_base.fillna(False)):
        if enter:
            state = 1.0
        elif exit_:
            state = BASE_POSITION
        target.append(state)
    return pd.Series(target, index=df.index, dtype=float)


def main() -> int:
    df = pd.read_csv(SCORE_HISTORY, dtype={"trade_date": str})
    df = df.sort_values("trade_date").dropna(subset=["close", "score"]).reset_index(drop=True)
    df["date"] = pd.to_datetime(df["trade_date"])
    df["daily_ret"] = df["close"].pct_change().fillna(0)
    df["ma15"] = df["close"].rolling(MA_WINDOW, min_periods=MA_WINDOW).mean()
    df["target_position"] = build_signal(df)
    df["position"] = df["target_position"].shift(1).fillna(1.0)
    df["turnover"] = (df["position"] - df["position"].shift(1).fillna(df["position"].iloc[0])).abs()
    df["strategy_ret"] = df["position"] * df["daily_ret"] - df["turnover"] * TURNOVER_COST
    df["buyhold_ret"] = df["daily_ret"]
    df["strategy_nav"] = (1 + df["strategy_ret"]).cumprod()
    df["buyhold_nav"] = (1 + df["buyhold_ret"]).cumprod()

    full_strategy = metric(df, "strategy_ret", "strategy_nav")
    full_buyhold = metric(df, "buyhold_ret", "buyhold_nav")
    segments = pd.DataFrame(
        [
            {"segment": "2007-2012", **segment(df, "2007-01-01", "2012-12-31")},
            {"segment": "2013-2018", **segment(df, "2013-01-01", "2018-12-31")},
            {"segment": "2016-2020", **segment(df, "2016-01-01", "2020-12-31")},
            {"segment": "2019-2026", **segment(df, "2019-01-01")},
            {"segment": "2021-2026", **segment(df, "2021-01-01")},
        ]
    )

    costs = []
    for cost in [0, 0.0005, 0.001, 0.002, 0.003, 0.005]:
        ret = df["position"] * df["daily_ret"] - df["turnover"] * cost
        nav = (1 + ret).cumprod()
        tmp = df.copy()
        tmp["tmp_ret"] = ret
        tmp["tmp_nav"] = nav
        costs.append({"turnover_cost": cost, **metric(tmp, "tmp_ret", "tmp_nav")})
    costs = pd.DataFrame(costs)

    latest = df.iloc[-1]
    signal = {
        "trade_date": latest["trade_date"],
        "close": float(latest["close"]),
        "score": float(latest["score"]),
        "ma15": float(latest["ma15"]),
        "upper_band": float(latest["ma15"] * (1 + BAND)),
        "lower_band": float(latest["ma15"] * (1 - BAND)),
        "target_position": float(latest["target_position"]),
        "applied_position": float(latest["position"]),
        "low_score_trigger": bool(latest["score"] <= LOW_SCORE),
        "trend_enter_trigger": bool(latest["close"] > latest["ma15"] * (1 + BAND)),
        "base_exit_trigger": bool((latest["score"] > LOW_SCORE) and (latest["close"] < latest["ma15"] * (1 - BAND))),
        "rule": (
            f"100% if score <= {LOW_SCORE} or close > MA{MA_WINDOW}*(1+{BAND:.0%}); "
            f"{BASE_POSITION:.0%} if score > {LOW_SCORE} and close < MA{MA_WINDOW}*(1-{BAND:.0%}); "
            "otherwise hold previous target"
        ),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    summary = {
        "rule_name": "H30269 recommended band strategy",
        "base_position": BASE_POSITION,
        "ma_window": MA_WINDOW,
        "band": BAND,
        "low_score": LOW_SCORE,
        "turnover_cost": TURNOVER_COST,
        "start_date": df["trade_date"].iloc[0],
        "end_date": df["trade_date"].iloc[-1],
        "strategy": full_strategy,
        "buyhold": full_buyhold,
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
        "score",
        "ma15",
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
    (ANALYSIS_DIR / "h30269_recommended_strategy_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (ANALYSIS_DIR / "h30269_recommended_strategy_signal.json").write_text(
        json.dumps(signal, ensure_ascii=False, indent=2), encoding="utf-8"
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

    report = f"""# H30269 推荐策略回测

生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

## 推荐规则

- 评分 <= {LOW_SCORE}：目标仓位 100%
- 否则，收盘价 > MA{MA_WINDOW} 上轨 {1+BAND:.2f} 倍：目标仓位 100%
- 否则，评分 > {LOW_SCORE} 且收盘价 < MA{MA_WINDOW} 下轨 {1-BAND:.2f} 倍：目标仓位 {pct(BASE_POSITION)}
- 其余情况：保持上一目标仓位

信号收盘后确认，下一交易日生效。回测扣除 {pct(TURNOVER_COST)} 的仓位变动成本。

## 全样本结果

- 样本区间：{summary['start_date']} 至 {summary['end_date']}，约 {full_strategy['years']:.2f} 年
- 策略年化收益：{pct(full_strategy['cagr'])}
- 同区间持有不动年化收益：{pct(full_buyhold['cagr'])}
- 策略累计收益：{pct(full_strategy['total_return'])}
- 同区间持有不动累计收益：{pct(full_buyhold['total_return'])}
- 策略最大回撤：{pct(full_strategy['max_drawdown'])}
- 同区间持有不动最大回撤：{pct(full_buyhold['max_drawdown'])}
- 策略年化波动：{pct(full_strategy['annual_vol'])}
- 同区间持有不动年化波动：{pct(full_buyhold['annual_vol'])}
- 平均仓位：{pct(summary['avg_exposure'])}
- 仓位变化：{summary['position_changes']} 次，约 {summary['avg_changes_per_year']:.1f} 次/年
- 年均换手：{summary['avg_turnover_per_year']:.1f} 倍目标资金

## 分段检验

|区间|策略年化|持有年化|策略最大回撤|持有最大回撤|平均仓位|仓位变化|
|---|---:|---:|---:|---:|---:|---:|
{chr(10).join(segment_lines)}

## 成本敏感性

|单次仓位变动成本|策略年化|最大回撤|累计收益|
|---:|---:|---:|---:|
{chr(10).join(cost_lines)}

## 当前信号

- 最新交易日：{signal['trade_date']}
- 收盘点位：{num(signal['close'], 2)}
- 当前评分：{num(signal['score'], 2)}
- MA{MA_WINDOW}：{num(signal['ma15'], 2)}
- 上轨：{num(signal['upper_band'], 2)}
- 下轨：{num(signal['lower_band'], 2)}
- 低分触发：{'是' if signal['low_score_trigger'] else '否'}
- 上轨触发：{'是' if signal['trend_enter_trigger'] else '否'}
- 下轨降仓触发：{'是' if signal['base_exit_trigger'] else '否'}
- 下一交易日目标仓位：{pct(signal['target_position'])}
"""
    (ANALYSIS_DIR / "h30269_recommended_strategy_report.md").write_text(report, encoding="utf-8")
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
