#!/usr/bin/env python3
"""Build a user-facing combined H30269 report focused on action prompts."""

from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd


BASE_DIR = Path(__file__).resolve().parents[1]
ANALYSIS_DIR = BASE_DIR / "analysis" / "h30269"


def pct(value: float | None) -> str:
    if value is None or not np.isfinite(value):
        return "-"
    return f"{value * 100:.2f}%"


def pct_plain(value: float | None) -> str:
    if value is None or not np.isfinite(value):
        return "-"
    return f"{value:.2f}%"


def num(value: float | None, digits: int = 2) -> str:
    if value is None or not np.isfinite(value):
        return "-"
    return f"{value:.{digits}f}"


def ymd(value: str) -> str:
    text = str(value)
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return text


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def markdown_table(df: pd.DataFrame, columns: list[str]) -> str:
    rows = ["|" + "|".join(columns) + "|", "|" + "|".join(["---"] * len(columns)) + "|"]
    for _, row in df.iterrows():
        rows.append("|" + "|".join(str(row.get(col, "")) for col in columns) + "|")
    return "\n".join(rows)


def action_prompt(current_position: float, target_position: float) -> str:
    diff = target_position - current_position
    if abs(diff) < 0.005:
        return f"维持 {pct(target_position)} 仓位"
    if diff > 0:
        return f"加仓到 {pct(target_position)}"
    return f"降仓到 {pct(target_position)}"


def score_zone(score: float) -> str:
    if score <= 3:
        return "高胜率买入观察区"
    if score >= 7:
        return "高分卖出/降仓观察区"
    if score <= 4.0:
        return "偏低估保护区"
    return "中性区"


def reason_text(signal: dict) -> str:
    if not signal.get("rebalance_due", False):
        return "本日不是月度确认日：按推荐规则保持上一月度目标仓位。"
    if signal.get("low_score_trigger"):
        return "低分保护成立：评分 <= 4.0，按推荐规则切换或维持满仓。"
    if signal.get("trend_trigger"):
        return f"趋势条件成立：收盘价站上 MA{signal.get('ma_window', '')}，按推荐规则切换或维持满仓。"
    return "月度确认日未触发低分或趋势条件：按推荐规则退出到现金管理。"


def next_trigger_text(signal: dict) -> str:
    ma_window = signal.get("ma_window", "")
    if signal["target_position"] >= 0.999:
        return (
            f"后续月度确认时，若评分升到 4.0 以上且收盘价低于 MA{ma_window}，策略会退出到现金管理。"
            f"若评分维持低位或收盘价站上 MA{ma_window}，则继续满仓。"
        )
    return (
        f"后续月度确认时，若评分回落到 4.0 以下，或收盘价站上 MA{ma_window}，策略会加仓到 100%。"
    )


def main() -> int:
    score = read_json(ANALYSIS_DIR / "h30269_latest_score.json")
    recommended = read_json(ANALYSIS_DIR / "h30269_recommended_strategy_summary.json")
    signal = recommended["current_signal"]

    nav = pd.read_csv(ANALYSIS_DIR / "h30269_recommended_strategy_nav.csv", dtype={"trade_date": str})
    latest_nav = nav.iloc[-1]
    current_position = float(latest_nav["position"])
    target_position = float(signal["target_position"])

    components = pd.read_csv(ANALYSIS_DIR / "h30269_score_components_latest.csv")
    components_out = components.copy()
    components_out["当前值"] = components_out.apply(format_component_value, axis=1)
    components_out["子分"] = components_out["score"].map(lambda x: num(x, 2))
    components_out["权重"] = components_out["weight"].map(pct)
    components_out["贡献"] = components_out["contribution"].map(lambda x: num(x, 2))
    components_out = components_out.rename(columns={"component": "指标", "metric": "口径"})
    components_out = components_out[["指标", "口径", "当前值", "子分", "权重", "贡献"]]

    backtest = pd.read_csv(ANALYSIS_DIR / "h30269_score_backtest.csv")
    backtest_120 = backtest[backtest["horizon_days"] == 120].copy()
    backtest_120["胜率"] = backtest_120["win_rate"].map(pct)
    backtest_120["平均收益"] = backtest_120["avg_return"].map(pct)
    backtest_120["中位收益"] = backtest_120["median_return"].map(pct)
    backtest_120 = backtest_120.rename(columns={"zone": "评分区间", "samples": "样本数"})
    backtest_120 = backtest_120[["评分区间", "样本数", "胜率", "平均收益", "中位收益"]]

    segments = pd.read_csv(ANALYSIS_DIR / "h30269_recommended_strategy_segments.csv")
    segments_out = segments.copy()
    segments_out["策略年化"] = segments_out["strategy_cagr"].map(pct)
    segments_out["持有年化"] = segments_out["buyhold_cagr"].map(pct)
    segments_out["策略回撤"] = segments_out["strategy_mdd"].map(pct)
    segments_out["持有回撤"] = segments_out["buyhold_mdd"].map(pct)
    segments_out["平均仓位"] = segments_out["strategy_exposure"].map(pct)
    segments_out = segments_out.rename(columns={"segment": "区间", "changes": "仓位变化"})
    segments_out = segments_out[["区间", "策略年化", "持有年化", "策略回撤", "持有回撤", "平均仓位", "仓位变化"]]

    constituents = pd.read_csv(ANALYSIS_DIR / "h30269_constituents_latest.csv")
    top_constituents = constituents.head(10).copy()
    top_constituents["名称"] = top_constituents["name"].fillna(top_constituents["con_code"])
    top_constituents["权重"] = top_constituents["weight"].map(lambda x: f"{x:.3f}%")
    top_constituents["PE"] = top_constituents["pe_for_calc"].map(lambda x: num(x, 2))
    top_constituents["年化ROE"] = top_constituents["roe_for_calc"].map(lambda x: pct_plain(x))
    top_constituents["股息率"] = top_constituents["dv_ttm"].map(lambda x: pct_plain(x))
    top_constituents = top_constituents.rename(columns={"con_code": "代码", "industry": "行业"})
    top_constituents = top_constituents[["代码", "名称", "行业", "权重", "PE", "年化ROE", "股息率"]]

    industry = pd.read_csv(ANALYSIS_DIR / "h30269_industry_weights_latest.csv").head(8)
    industry["权重"] = industry["weight"].map(lambda x: f"{x:.2f}%")
    industry = industry.rename(columns={"industry": "行业"})[["行业", "权重"]]

    strategy = recommended["strategy"]
    buyhold = recommended["buyhold"]
    research = read_json(ANALYSIS_DIR / "h30269_strategy_research_summary.json")
    score_value = float(signal["score"])
    action = action_prompt(current_position, target_position)
    ma_window = int(signal.get("ma_window", recommended.get("ma_window", 0)))
    cash_return = float(recommended.get("cash_annual_return", 0.0))
    turnover_cost = float(recommended.get("turnover_cost", 0.0))
    low_score = float(recommended.get("low_score", 4.0))

    prompt_lines = [
        f"当前提示：**{action}**。",
        f"评分状态：{num(score_value, 2)} / 10，属于**{score_zone(score_value)}**。",
        f"触发原因：{reason_text(signal)}",
        f"下一触发条件：{next_trigger_text(signal)}",
    ]

    if abs(target_position - current_position) >= 0.005:
        prompt_lines.append(
            f"如果你当前严格跟随策略，上一交易日应为 {pct(current_position)}，下一交易日目标为 {pct(target_position)}。"
        )
    else:
        prompt_lines.append(f"如果你当前严格跟随策略，目前不需要调仓，目标仍为 {pct(target_position)}。")

    report = f"""# 红利低波 H30269 行动报告

生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

## 给你的提示

- {prompt_lines[0]}
- {prompt_lines[1]}
- {prompt_lines[2]}
- {prompt_lines[3]}
- {prompt_lines[4]}

## 当前状态

- 指数代码：H30269.CSI
- 最新交易日：{ymd(signal['trade_date'])}
- 最新点位：{num(signal['close'], 2)}
- 当前评分：{num(score_value, 2)} / 10
- MA{ma_window}：{num(signal['ma'], 2)}
- 是否月度确认日：{'是' if signal.get('rebalance_due') else '否'}
- 是否触发低分保护：{'是' if signal['low_score_trigger'] else '否'}
- 是否站上 MA{ma_window}：{'是' if signal.get('trend_trigger') else '否'}
- 如果今天确认，目标仓位：{pct(signal.get('raw_target_if_rebalanced_today'))}
- 当前策略仓位：{pct(current_position)}
- 下一交易日目标仓位：{pct(target_position)}

## 策略规则

使用系统研究后选出的推荐策略作为主提示：

- 每月最后一个交易日收盘后确认，下一交易日执行
- 评分 <= {num(low_score, 1)}：目标仓位 100%
- 否则，收盘价 > MA{ma_window}：目标仓位 100%
- 否则：目标仓位 {pct(recommended['base_position'])}，未投入 H30269 的资金进入现金管理
- 回测扣除 {pct(turnover_cost)} 的仓位变动成本，并假设空仓资金年化 {pct(cash_return)}
- 策略与持有收益均按红利低波全收益指数 h20269.CSI（含股息再投资）计算；点位与信号仍按价格指数口径

旧的“3分买入、7分卖出”只作为情绪温度计，不再作为主策略。现金管理收益是正式口径的一部分；全收益口径下，本策略近年的主要贡献是降低回撤而非增厚收益。

## 策略回测（全收益口径）

- 策略样本区间：{recommended['start_date']} 至 {recommended['end_date']}
- 推荐策略年化收益：{pct(strategy['cagr'])}
- 同区间持有不动年化收益：{pct(buyhold['cagr'])}
- 推荐策略年化超额：{pct(recommended.get('excess_cagr_vs_buyhold'))}
- 推荐策略累计收益：{pct(strategy['total_return'])}
- 同区间持有不动累计收益：{pct(buyhold['total_return'])}
- 推荐策略最大回撤：{pct(strategy['max_drawdown'])}
- 同区间持有不动最大回撤：{pct(buyhold['max_drawdown'])}
- 平均仓位：{pct(recommended['avg_exposure'])}
- 仓位变化：{recommended['position_changes']} 次，约 {recommended['avg_changes_per_year']:.1f} 次/年
- 年均换手：{recommended['avg_turnover_per_year']:.1f} 倍目标资金

研究口径：策略来自 {research['candidate_count']} 个候选的系统研究，并于 2026-06 经过 IS/OOS 切分与 walk-forward 防过拟合复检（见 analysis/h30269/robust_research/）；2026-06-10 起回测基准切换为全收益指数。策略比较从无未来函数评分首次可用日 {recommended['start_date']} 开始。

## 分段检验

{markdown_table(segments_out, list(segments_out.columns))}

## 评分依据

{markdown_table(components_out, list(components_out.columns))}

## 评分区间历史胜率

未来 120 个交易日口径：

{markdown_table(backtest_120, list(backtest_120.columns))}

## 成分估值与集中度

- 成分数量：{len(constituents)}
- 前十大权重合计：{num(constituents.head(10)['weight'].sum(), 2)}%
- 成分加权 PE(TTM)：{num(weighted_average(constituents, 'pe_for_calc'), 2)}
- 成分加权年化 ROE：{pct_plain(weighted_average(constituents, 'roe_for_calc'))}
- 成分加权股息率 TTM：{pct_plain(weighted_average(constituents, 'dv_ttm'))}

### 前十大成分

{markdown_table(top_constituents, list(top_constituents.columns))}

### 行业权重前八

{markdown_table(industry, list(industry.columns))}

## 使用提醒

这份报告只给仓位提示，不保证收益。实际执行时，建议固定一个目标资金规模，按目标仓位做再平衡；如果使用 ETF，还要看基金折溢价、流动性、交易费率和跟踪误差。
"""

    out = ANALYSIS_DIR / "h30269_combined_report.md"
    out.write_text(report, encoding="utf-8")
    print(out)
    print(action)
    return 0


def format_component_value(row: pd.Series) -> str:
    metric = str(row["metric"])
    value = float(row["value"])
    if "分位" in metric or "RSI" in metric:
        return num(value, 2)
    return pct(value)


def weighted_average(df: pd.DataFrame, col: str) -> float:
    valid = df[[col, "weight"]].dropna()
    valid = valid[np.isfinite(valid[col])]
    if valid.empty:
        return np.nan
    return float((valid[col] * valid["weight"]).sum() / valid["weight"].sum())


if __name__ == "__main__":
    raise SystemExit(main())
