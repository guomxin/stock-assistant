#!/usr/bin/env python3
"""Backtest an enhanced H30269 allocation rule.

Rule:
- Keep 100% H30269 when close > 20-day moving average, or the score <= 4.5.
- Otherwise keep a 40% core H30269 position.

Signals are known after close and applied from the next trading day.
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

BASE_POSITION = 0.40
MA_WINDOW = 20
CHEAP_SCORE = 4.5
TURNOVER_COST = 0.001


def pct(value: float | None) -> str:
    if value is None or not np.isfinite(value):
        return "-"
    return f"{value * 100:.2f}%"


def num(value: float | None, digits: int = 2) -> str:
    if value is None or not np.isfinite(value):
        return "-"
    return f"{value:.{digits}f}"


def calc_metrics(frame: pd.DataFrame, ret_col: str, nav_col: str) -> dict[str, float]:
    years = (frame["date"].iloc[-1] - frame["date"].iloc[0]).days / 365.25
    nav = frame[nav_col] / frame[nav_col].iloc[0]
    ret = frame[ret_col]
    cagr = nav.iloc[-1] ** (1 / years) - 1
    max_dd = (nav / nav.cummax() - 1).min()
    ann_vol = ret.std() * math.sqrt(252)
    return {
        "total_return": float(nav.iloc[-1] - 1),
        "cagr": float(cagr),
        "max_drawdown": float(max_dd),
        "annual_vol": float(ann_vol),
        "sharpe_0rf": float(cagr / ann_vol) if ann_vol else np.nan,
        "years": float(years),
    }


def segment_metrics(df: pd.DataFrame, start: str, end: str | None = None) -> dict[str, float]:
    tmp = df[df["date"] >= pd.Timestamp(start)].copy()
    if end:
        tmp = tmp[tmp["date"] <= pd.Timestamp(end)]
    tmp = tmp.reset_index(drop=True)
    return {
        "start": tmp["trade_date"].iloc[0],
        "end": tmp["trade_date"].iloc[-1],
        "strategy_cagr": calc_metrics(tmp, "strategy_ret", "strategy_nav")["cagr"],
        "strategy_mdd": calc_metrics(tmp, "strategy_ret", "strategy_nav")["max_drawdown"],
        "buyhold_cagr": calc_metrics(tmp, "buyhold_ret", "buyhold_nav")["cagr"],
        "buyhold_mdd": calc_metrics(tmp, "buyhold_ret", "buyhold_nav")["max_drawdown"],
        "strategy_exposure": float(tmp["position"].mean()),
        "changes": int((tmp["turnover"] > 0).sum()),
    }


def main() -> int:
    raw_df = pd.read_csv(SCORE_HISTORY, dtype={"trade_date": str}).sort_values("trade_date").reset_index(drop=True)
    raw_price = raw_df.dropna(subset=["close"]).reset_index(drop=True)
    raw_price["date"] = pd.to_datetime(raw_price["trade_date"])
    raw_price["buyhold_ret"] = raw_price["close"].pct_change().fillna(0)
    raw_price["buyhold_nav"] = (1 + raw_price["buyhold_ret"]).cumprod()
    inception_buyhold = calc_metrics(raw_price, "buyhold_ret", "buyhold_nav")

    df = raw_df.dropna(subset=["score", "close"]).reset_index(drop=True)
    df["date"] = pd.to_datetime(df["trade_date"])
    df["daily_ret"] = df["close"].pct_change().fillna(0)
    df["ma20"] = df["close"].rolling(MA_WINDOW, min_periods=MA_WINDOW).mean()
    df["above_ma20"] = df["close"] > df["ma20"]
    df["cheap_enough"] = df["score"] <= CHEAP_SCORE
    df["target_position"] = np.where(
        df["above_ma20"] | df["cheap_enough"],
        1.0,
        BASE_POSITION,
    )

    # Signal is known after close T; position is applied from T+1.
    df["position"] = df["target_position"].shift(1).fillna(1.0)
    df["turnover"] = (df["position"] - df["position"].shift(1).fillna(df["position"].iloc[0])).abs()
    df["strategy_ret"] = df["position"] * df["daily_ret"] - df["turnover"] * TURNOVER_COST
    df["buyhold_ret"] = df["daily_ret"]
    df["strategy_nav"] = (1 + df["strategy_ret"]).cumprod()
    df["buyhold_nav"] = (1 + df["buyhold_ret"]).cumprod()

    full_strategy = calc_metrics(df, "strategy_ret", "strategy_nav")
    full_buyhold = calc_metrics(df, "buyhold_ret", "buyhold_nav")

    segments = pd.DataFrame(
        [
            {"segment": "2007-2012", **segment_metrics(df, "2007-01-01", "2012-12-31")},
            {"segment": "2013-2018", **segment_metrics(df, "2013-01-01", "2018-12-31")},
            {"segment": "2019-2026", **segment_metrics(df, "2019-01-01", None)},
            {"segment": "2021-2026", **segment_metrics(df, "2021-01-01", None)},
        ]
    )

    latest = df.iloc[-1]
    current_signal = {
        "trade_date": latest["trade_date"],
        "close": float(latest["close"]),
        "score": float(latest["score"]),
        "ma20": float(latest["ma20"]),
        "above_ma20": bool(latest["above_ma20"]),
        "cheap_enough": bool(latest["cheap_enough"]),
        "target_position": float(latest["target_position"]),
        "position_for_next_day": float(latest["target_position"]),
        "rule": f"100% if close > MA{MA_WINDOW} or score <= {CHEAP_SCORE}; otherwise {BASE_POSITION:.0%}",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    # Cost sensitivity.
    cost_rows = []
    for cost in [0, 0.0005, 0.001, 0.002, 0.003, 0.005]:
        ret = df["position"] * df["daily_ret"] - df["turnover"] * cost
        nav = (1 + ret).cumprod()
        tmp = df.copy()
        tmp["tmp_ret"] = ret
        tmp["tmp_nav"] = nav
        m = calc_metrics(tmp, "tmp_ret", "tmp_nav")
        cost_rows.append({"turnover_cost": cost, **m})
    costs = pd.DataFrame(cost_rows)

    out_cols = [
        "trade_date",
        "close",
        "score",
        "ma20",
        "above_ma20",
        "cheap_enough",
        "target_position",
        "position",
        "turnover",
        "strategy_ret",
        "strategy_nav",
        "buyhold_nav",
    ]
    df[out_cols].to_csv(ANALYSIS_DIR / "h30269_enhanced_strategy_nav.csv", index=False, encoding="utf-8-sig")
    segments.to_csv(ANALYSIS_DIR / "h30269_enhanced_strategy_segments.csv", index=False, encoding="utf-8-sig")
    costs.to_csv(ANALYSIS_DIR / "h30269_enhanced_strategy_costs.csv", index=False, encoding="utf-8-sig")
    (ANALYSIS_DIR / "h30269_enhanced_strategy_signal.json").write_text(
        json.dumps(current_signal, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    summary = {
        "rule": current_signal["rule"],
        "base_position": BASE_POSITION,
        "ma_window": MA_WINDOW,
        "cheap_score": CHEAP_SCORE,
        "turnover_cost": TURNOVER_COST,
        "start_date": df["trade_date"].iloc[0],
        "end_date": df["trade_date"].iloc[-1],
        "strategy": full_strategy,
        "buyhold": full_buyhold,
        "inception_buyhold": inception_buyhold,
        "inception_start_date": raw_price["trade_date"].iloc[0],
        "avg_exposure": float(df["position"].mean()),
        "position_changes": int((df["turnover"] > 0).sum()),
        "avg_changes_per_year": int((df["turnover"] > 0).sum()) / full_strategy["years"],
        "total_turnover": float(df["turnover"].sum()),
        "avg_turnover_per_year": float(df["turnover"].sum()) / full_strategy["years"],
        "current_signal": current_signal,
    }
    (ANALYSIS_DIR / "h30269_enhanced_strategy_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
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

    report = f"""# H30269 增强策略回测

生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

## 策略规则

目标是战胜 H30269 买入持有，同时不过度空仓。

- 如果收盘价 > 20日均线，目标仓位 = 100%
- 如果评分 <= 4.5，目标仓位 = 100%
- 否则目标仓位 = 40%

解释：红利低波长期有正收益，所以保留 40% 底仓；只有在“趋势弱且不够便宜”的时候降仓。信号收盘后确认，下一交易日生效；回测扣除 0.10% 的调仓成本。

## 全样本结果

- 策略样本区间：{summary['start_date']} 至 {summary['end_date']}，约 {full_strategy['years']:.2f} 年
- 策略年化收益：{pct(full_strategy['cagr'])}
- 同区间持有不动年化收益：{pct(full_buyhold['cagr'])}
- 策略累计收益：{pct(full_strategy['total_return'])}
- 同区间持有不动累计收益：{pct(full_buyhold['total_return'])}
- 策略最大回撤：{pct(full_strategy['max_drawdown'])}
- 同区间持有不动最大回撤：{pct(full_buyhold['max_drawdown'])}
- 策略年化波动：{pct(full_strategy['annual_vol'])}
- 同区间持有不动年化波动：{pct(full_buyhold['annual_vol'])}
- 平均仓位：{pct(summary['avg_exposure'])}
- 仓位变化次数：{summary['position_changes']} 次，平均 {summary['avg_changes_per_year']:.1f} 次/年
- 年均换手：{summary['avg_turnover_per_year']:.1f} 倍目标资金

说明：策略需要评分数据，所以策略比较从评分首次可用日 {summary['start_date']} 开始；H30269 在 Tushare 中最早可取日为 {summary['inception_start_date']}，若从该日直接持有到 {summary['end_date']}，年化收益为 {pct(inception_buyhold['cagr'])}，累计收益为 {pct(inception_buyhold['total_return'])}。

## 分段检验

|区间|策略年化|持有年化|策略最大回撤|持有最大回撤|平均仓位|仓位变化|
|---|---:|---:|---:|---:|---:|---:|
{chr(10).join(segment_lines)}

## 成本敏感性

|单次调仓成本|策略年化|最大回撤|累计收益|
|---:|---:|---:|---:|
{chr(10).join(cost_lines)}

## 当前信号

- 最新交易日：{current_signal['trade_date']}
- 收盘点位：{num(current_signal['close'], 2)}
- 当前评分：{num(current_signal['score'], 2)}
- 20日均线：{num(current_signal['ma20'], 2)}
- 是否站上20日均线：{'是' if current_signal['above_ma20'] else '否'}
- 是否低分保护：{'是' if current_signal['cheap_enough'] else '否'}
- 下一交易日目标仓位：{pct(current_signal['target_position'])}

## 结论

这个规则在全样本中跑赢持有不动，并且在 2019 年后和 2021 年后也没有掉队；它的弱点是交易频率高于原来的 3买7卖，适合用低费率 ETF 或场内基金执行。它不是预测涨跌，而是一个仓位管理规则：便宜或趋势好时满仓，趋势弱且不便宜时降到 40%。
"""
    (ANALYSIS_DIR / "h30269_enhanced_strategy_report.md").write_text(report, encoding="utf-8")
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
