#!/usr/bin/env python3
"""Backtest the H30269 score rule: buy <= 3, sell >= 7."""

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


def pct(value: float) -> str:
    if value is None or not np.isfinite(value):
        return "-"
    return f"{value * 100:.2f}%"


def max_drawdown(nav: pd.Series) -> float:
    return float((nav / nav.cummax() - 1).min())


def cagr(nav: pd.Series, years: float) -> float:
    return float(nav.iloc[-1] ** (1 / years) - 1)


def main() -> int:
    df = pd.read_csv(SCORE_HISTORY, dtype={"trade_date": str})
    df = df.sort_values("trade_date").dropna(subset=["score", "close"]).reset_index(drop=True)
    df["date"] = pd.to_datetime(df["trade_date"])
    df["daily_ret"] = df["close"].pct_change().fillna(0)

    trades: list[dict] = []
    pos = 0
    signal_position = []
    for _, row in df.iterrows():
        if pos == 0 and row["score"] <= 3:
            pos = 1
            trades.append(
                {
                    "action": "BUY",
                    "signal_date": row["trade_date"],
                    "signal_score": float(row["score"]),
                    "signal_close": float(row["close"]),
                }
            )
        elif pos == 1 and row["score"] >= 7:
            pos = 0
            trades.append(
                {
                    "action": "SELL",
                    "signal_date": row["trade_date"],
                    "signal_score": float(row["score"]),
                    "signal_close": float(row["close"]),
                }
            )
        signal_position.append(pos)

    # Signal is known after close T; apply it from the next trading day to avoid same-close bias.
    df["signal_position"] = signal_position
    df["position"] = df["signal_position"].shift(1).fillna(0)
    df["strategy_ret"] = df["position"] * df["daily_ret"]
    df["strategy_nav"] = (1 + df["strategy_ret"]).cumprod()
    df["buyhold_nav"] = (1 + df["daily_ret"]).cumprod()

    trade_df = pd.DataFrame(trades)
    if not trade_df.empty:
        date_to_idx = {d: i for i, d in enumerate(df["trade_date"])}
        exec_rows = []
        for trade in trades:
            idx = date_to_idx[trade["signal_date"]]
            exec_idx = min(idx + 1, len(df) - 1)
            exec_rows.append(
                {
                    "exec_date": df.loc[exec_idx, "trade_date"],
                    "exec_close": float(df.loc[exec_idx, "close"]),
                }
            )
        trade_df = pd.concat([trade_df, pd.DataFrame(exec_rows)], axis=1)

    cycles = []
    open_buy = None
    for _, row in trade_df.iterrows():
        if row["action"] == "BUY":
            open_buy = row
        elif row["action"] == "SELL" and open_buy is not None:
            buy_date = pd.to_datetime(open_buy["exec_date"])
            sell_date = pd.to_datetime(row["exec_date"])
            ret = row["exec_close"] / open_buy["exec_close"] - 1
            cycles.append(
                {
                    "buy_signal_date": open_buy["signal_date"],
                    "buy_exec_date": open_buy["exec_date"],
                    "buy_score": open_buy["signal_score"],
                    "buy_price": open_buy["exec_close"],
                    "sell_signal_date": row["signal_date"],
                    "sell_exec_date": row["exec_date"],
                    "sell_score": row["signal_score"],
                    "sell_price": row["exec_close"],
                    "hold_days": int((sell_date - buy_date).days),
                    "return": float(ret),
                }
            )
            open_buy = None
    cycle_df = pd.DataFrame(cycles)

    years = (df["date"].iloc[-1] - df["date"].iloc[0]).days / 365.25
    strategy_nav = df["strategy_nav"]
    buyhold_nav = df["buyhold_nav"]
    buy_count = int((trade_df["action"] == "BUY").sum()) if not trade_df.empty else 0
    sell_count = int((trade_df["action"] == "SELL").sum()) if not trade_df.empty else 0
    round_trips = len(cycle_df)

    summary = {
        "start_date": df["trade_date"].iloc[0],
        "end_date": df["trade_date"].iloc[-1],
        "years": years,
        "buy_signals": buy_count,
        "sell_signals": sell_count,
        "round_trips": round_trips,
        "trade_actions_per_year": (buy_count + sell_count) / years,
        "buy_signals_per_year": buy_count / years,
        "round_trips_per_year": round_trips / years,
        "strategy_total_return": float(strategy_nav.iloc[-1] - 1),
        "strategy_cagr": cagr(strategy_nav, years),
        "strategy_max_drawdown": max_drawdown(strategy_nav),
        "strategy_annual_vol": float(df["strategy_ret"].std() * math.sqrt(252)),
        "strategy_exposure": float(df["position"].mean()),
        "buyhold_total_return": float(buyhold_nav.iloc[-1] - 1),
        "buyhold_cagr": cagr(buyhold_nav, years),
        "buyhold_max_drawdown": max_drawdown(buyhold_nav),
        "buyhold_annual_vol": float(df["daily_ret"].std() * math.sqrt(252)),
    }
    if not cycle_df.empty:
        summary.update(
            {
                "cycle_win_rate": float((cycle_df["return"] > 0).mean()),
                "avg_cycle_return": float(cycle_df["return"].mean()),
                "median_cycle_return": float(cycle_df["return"].median()),
                "avg_hold_days": float(cycle_df["hold_days"].mean()),
            }
        )
    if open_buy is not None:
        latest_close = float(df["close"].iloc[-1])
        summary.update(
            {
                "open_position": True,
                "open_buy_exec_date": open_buy["exec_date"],
                "open_buy_score": float(open_buy["signal_score"]),
                "open_buy_price": float(open_buy["exec_close"]),
                "open_latest_date": df["trade_date"].iloc[-1],
                "open_latest_close": latest_close,
                "open_unrealized_return": latest_close / open_buy["exec_close"] - 1,
            }
        )
    else:
        summary["open_position"] = False

    ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)
    trade_df.to_csv(ANALYSIS_DIR / "h30269_strategy_trades.csv", index=False, encoding="utf-8-sig")
    cycle_df.to_csv(ANALYSIS_DIR / "h30269_strategy_cycles.csv", index=False, encoding="utf-8-sig")
    (ANALYSIS_DIR / "h30269_strategy_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    cycle_lines = []
    if not cycle_df.empty:
        for _, row in cycle_df.iterrows():
            cycle_lines.append(
                f"|{row['buy_exec_date']}|{row['buy_score']:.2f}|{row['sell_exec_date']}|"
                f"{row['sell_score']:.2f}|{row['hold_days']}|{pct(row['return'])}|"
            )
    else:
        cycle_lines.append("|-|-|-|-|-|-|")

    open_line = ""
    if summary["open_position"]:
        open_line = (
            f"\n当前还有一笔未平仓持仓：{summary['open_buy_exec_date']} 买入，"
            f"买入评分 {summary['open_buy_score']:.2f}，截至 {summary['open_latest_date']} "
            f"浮动收益 {pct(summary['open_unrealized_return'])}。\n"
        )

    report = f"""# H30269 3买7卖策略回测

生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

规则：评分 <= 3 时买入，评分 >= 7 时卖出。信号在收盘后确认，下一交易日开始计入持仓收益；未计现金利息、基金跟踪误差、申赎费、佣金和税费。

## 核心结果

- 样本区间：{summary['start_date']} 至 {summary['end_date']}，约 {summary['years']:.2f} 年
- 买入信号：{summary['buy_signals']} 次，平均 {summary['buy_signals_per_year']:.2f} 次/年
- 完整买卖轮次：{summary['round_trips']} 次，平均 {summary['round_trips_per_year']:.2f} 次/年
- 买入+卖出动作：{summary['buy_signals'] + summary['sell_signals']} 次，平均 {summary['trade_actions_per_year']:.2f} 次/年
- 策略年化收益：{pct(summary['strategy_cagr'])}
- 策略累计收益：{pct(summary['strategy_total_return'])}
- 策略最大回撤：{pct(summary['strategy_max_drawdown'])}
- 策略平均持仓暴露：{pct(summary['strategy_exposure'])}
- 买入持有年化收益：{pct(summary['buyhold_cagr'])}
- 买入持有最大回撤：{pct(summary['buyhold_max_drawdown'])}
- 已平仓轮次胜率：{pct(summary.get('cycle_win_rate', np.nan))}
- 已平仓单轮平均收益：{pct(summary.get('avg_cycle_return', np.nan))}
- 已平仓单轮中位收益：{pct(summary.get('median_cycle_return', np.nan))}
- 平均持仓天数：{summary.get('avg_hold_days', np.nan):.0f} 天
{open_line}
## 历史买卖轮次

|买入执行日|买入评分|卖出执行日|卖出评分|持仓天数|区间收益|
|---|---:|---|---:|---:|---:|
{chr(10).join(cycle_lines)}
"""
    (ANALYSIS_DIR / "h30269_strategy_report.md").write_text(report, encoding="utf-8")
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
