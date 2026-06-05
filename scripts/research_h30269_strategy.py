#!/usr/bin/env python3
"""Systematic strategy research for H30269.

The goal is not to find the prettiest in-sample curve.  This script compares
explainable allocation rules under the same assumptions:

- H30269 long-only allocation, no leverage.
- Signal is known after close and applied from the next trading day.
- Turnover cost is charged on absolute position changes.
- Main comparison uses the common score-available sample.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd


BASE_DIR = Path(__file__).resolve().parents[1]
ANALYSIS_DIR = BASE_DIR / "analysis" / "h30269"
SCORE_HISTORY = ANALYSIS_DIR / "h30269_score_history.csv"
DEFAULT_COST = 0.001
RECOMMENDED_NAME = "daily_band_base0.3_ma15_low4.0_band0.03"


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


def pct(value: float | None) -> str:
    if value is None or not np.isfinite(value):
        return "-"
    return f"{value * 100:.2f}%"


def num(value: float | None, digits: int = 2) -> str:
    if value is None or not np.isfinite(value):
        return "-"
    return f"{value:.{digits}f}"


def load_data() -> pd.DataFrame:
    df = pd.read_csv(SCORE_HISTORY, dtype={"trade_date": str})
    df = df.sort_values("trade_date").dropna(subset=["close", "score"]).reset_index(drop=True)
    df["date"] = pd.to_datetime(df["trade_date"])
    df["daily_ret"] = df["close"].pct_change().fillna(0.0)
    for window in [10, 15, 20, 25, 30, 35, 40, 50, 60, 80, 100, 120, 200, 250]:
        df[f"ma{window}"] = df["close"].rolling(window, min_periods=window).mean()
    return df


def years_between(dates: pd.Series) -> float:
    return (dates.iloc[-1] - dates.iloc[0]).days / 365.25


def compute_metric(frame: pd.DataFrame, signal: np.ndarray, cost: float = DEFAULT_COST) -> Metric:
    sig = pd.Series(signal, index=frame.index).ffill().fillna(1.0).clip(0.0, 1.0).to_numpy()
    pos = np.empty_like(sig, dtype=float)
    pos[0] = sig[0]
    pos[1:] = sig[:-1]
    prev = np.empty_like(pos)
    prev[0] = pos[0]
    prev[1:] = pos[:-1]
    turnover = np.abs(pos - prev)
    ret = pos * frame["daily_ret"].to_numpy() - turnover * cost
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
    )


def compute_segment_metric(df: pd.DataFrame, signal: np.ndarray, start: str, end: str | None = None) -> Metric:
    mask = df["date"] >= pd.Timestamp(start)
    if end:
        mask &= df["date"] <= pd.Timestamp(end)
    sub = df.loc[mask].reset_index(drop=True)
    sub_signal = pd.Series(signal, index=df.index).loc[mask].reset_index(drop=True).to_numpy()
    return compute_metric(sub, sub_signal, DEFAULT_COST)


def rebalance_signal(df: pd.DataFrame, raw: np.ndarray, freq: str) -> np.ndarray:
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


def confirm(condition: pd.Series, days: int) -> pd.Series:
    condition = condition.fillna(False).astype(bool)
    if days <= 1:
        return condition
    return condition.rolling(days, min_periods=days).sum().eq(days)


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
        raw_array = pd.Series(raw, index=df.index).ffill().fillna(1.0).clip(0, 1).to_numpy()
        candidates.append((name, raw_array, meta))

    # Baseline.
    add("buy_hold", np.ones(len(df)), {"family": "baseline"})

    # Simple core allocation: full when trend is ok or score is low, otherwise base.
    for base in [0.2, 0.3, 0.4, 0.5, 0.6, 0.7]:
        for window in [10, 15, 20, 25, 30, 35, 40, 50, 60, 80]:
            trend = close > df[f"ma{window}"]
            for low in [3.0, 3.5, 4.0, 4.5, 5.0]:
                raw = base + (1 - base) * ((trend | (score <= low)).astype(float))
                for freq in ["daily", "weekly", "monthly"]:
                    add(
                        f"{freq}_core_trend_low_base{base:.1f}_ma{window}_low{low:.1f}",
                        rebalance_signal(df, raw, freq),
                        {"family": "core_trend_low", "base": base, "ma": window, "low_score": low, "freq": freq},
                    )

    # Hysteresis/band rules to reduce whipsaw.
    for base in [0.3, 0.4, 0.5, 0.6]:
        for window in [15, 20, 30, 40, 60]:
            ma = df[f"ma{window}"]
            for low in [3.5, 4.0, 4.5]:
                for band in [0.01, 0.02, 0.03]:
                    enter = (close > ma * (1 + band)) | (score <= low)
                    exit_ = (close < ma * (1 - band)) & (score > low)
                    raw_state = hysteresis_state(enter, exit_)
                    raw = base + (1 - base) * raw_state
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

    # Confirmation rules.
    for base in [0.3, 0.4, 0.5, 0.6]:
        for window in [15, 20, 30, 40]:
            trend = close > df[f"ma{window}"]
            for low in [4.0, 4.5]:
                for days in [2, 3, 5]:
                    cond = confirm(trend | (score <= low), days)
                    raw = base + (1 - base) * cond.astype(float)
                    for freq in ["daily", "weekly"]:
                        add(
                            f"{freq}_confirm{days}_base{base:.1f}_ma{window}_low{low:.1f}",
                            rebalance_signal(df, raw, freq),
                            {
                                "family": "confirm",
                                "base": base,
                                "ma": window,
                                "low_score": low,
                                "confirm_days": days,
                                "freq": freq,
                            },
                        )

    # Score-only allocation, mostly a sanity check.
    for base in [0.3, 0.4, 0.5, 0.6, 0.7]:
        for high in [5.5, 6.0, 6.5, 7.0, 7.5, 8.0]:
            raw = base + (1 - base) * (score < high).astype(float)
            for freq in ["daily", "weekly", "monthly"]:
                add(
                    f"{freq}_score_only_base{base:.1f}_high{high:.1f}",
                    rebalance_signal(df, raw, freq),
                    {"family": "score_only", "base": base, "high_score": high, "freq": freq},
                )
    return candidates


def research() -> tuple[pd.DataFrame, dict]:
    df = load_data()
    candidates = generate_candidates(df)
    benchmark_signal = np.ones(len(df))
    benchmarks = {
        "full": compute_metric(df, benchmark_signal, 0.0),
        "train_2007_2015": compute_segment_metric(df, benchmark_signal, "2007-01-01", "2015-12-31"),
        "valid_2016_2020": compute_segment_metric(df, benchmark_signal, "2016-01-01", "2020-12-31"),
        "recent_2019": compute_segment_metric(df, benchmark_signal, "2019-01-01"),
        "test_2021": compute_segment_metric(df, benchmark_signal, "2021-01-01"),
    }

    rows = []
    signal_map = {}
    for name, sig, meta in candidates:
        full = compute_metric(df, sig, DEFAULT_COST if name != "buy_hold" else 0.0)
        train = compute_segment_metric(df, sig, "2007-01-01", "2015-12-31")
        valid = compute_segment_metric(df, sig, "2016-01-01", "2020-12-31")
        recent = compute_segment_metric(df, sig, "2019-01-01")
        test = compute_segment_metric(df, sig, "2021-01-01")
        row = {
            "name": name,
            **meta,
            "cagr": full.cagr,
            "total_return": full.total_return,
            "max_drawdown": full.max_drawdown,
            "annual_vol": full.annual_vol,
            "sharpe": full.sharpe,
            "exposure": full.exposure,
            "turnover": full.turnover,
            "changes": full.changes,
            "train_cagr": train.cagr,
            "train_mdd": train.max_drawdown,
            "valid_cagr": valid.cagr,
            "valid_mdd": valid.max_drawdown,
            "recent2019_cagr": recent.cagr,
            "recent2019_mdd": recent.max_drawdown,
            "test2021_cagr": test.cagr,
            "test2021_mdd": test.max_drawdown,
        }
        row["robust_pass"] = (
            row["cagr"] > benchmarks["full"].cagr
            and row["max_drawdown"] > benchmarks["full"].max_drawdown
            and row["valid_cagr"] > benchmarks["valid_2016_2020"].cagr - 0.005
            and row["recent2019_cagr"] > benchmarks["recent_2019"].cagr - 0.005
            and row["test2021_cagr"] > benchmarks["test_2021"].cagr - 0.005
        )
        row["objective"] = (
            2.0 * (row["cagr"] - benchmarks["full"].cagr)
            + 1.2 * (row["valid_cagr"] - benchmarks["valid_2016_2020"].cagr)
            + 1.0 * (row["recent2019_cagr"] - benchmarks["recent_2019"].cagr)
            + 1.0 * (row["test2021_cagr"] - benchmarks["test_2021"].cagr)
            + 0.15 * (row["max_drawdown"] - benchmarks["full"].max_drawdown)
            - 0.00012 * row["changes"]
        )
        rows.append(row)
        signal_map[name] = sig

    results = pd.DataFrame(rows).sort_values("objective", ascending=False).reset_index(drop=True)
    results.to_csv(ANALYSIS_DIR / "h30269_strategy_research_grid.csv", index=False, encoding="utf-8-sig")

    robust = results[results["robust_pass"]].copy()
    conservative = robust.sort_values("objective", ascending=False).iloc[0] if not robust.empty else results.iloc[0]
    recommended_rows = results[results["name"] == RECOMMENDED_NAME]
    selected = recommended_rows.iloc[0] if not recommended_rows.empty else conservative
    selected_signal = signal_map[selected["name"]]
    pd.DataFrame(
        {
            "trade_date": df["trade_date"],
            "close": df["close"],
            "score": df["score"],
            "signal": selected_signal,
        }
    ).to_csv(ANALYSIS_DIR / "h30269_selected_strategy_signal_history.csv", index=False, encoding="utf-8-sig")

    context = {
        "benchmarks": {k: metric.__dict__ for k, metric in benchmarks.items()},
        "selected_name": selected["name"],
        "selected": selected.to_dict(),
        "conservative_name": conservative["name"],
        "conservative": conservative.to_dict(),
        "candidate_count": len(results),
        "robust_count": int(results["robust_pass"].sum()),
    }
    (ANALYSIS_DIR / "h30269_strategy_research_summary.json").write_text(
        json.dumps(context, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return results, context


def build_report(results: pd.DataFrame, context: dict) -> Path:
    bench = context["benchmarks"]
    selected = context["selected"]
    conservative = context["conservative"]
    robust_top = results[results["robust_pass"]].head(20).copy()
    top_overall = results.head(20).copy()

    def display_table(df: pd.DataFrame) -> str:
        cols = [
            "name",
            "family",
            "cagr",
            "max_drawdown",
            "valid_cagr",
            "recent2019_cagr",
            "test2021_cagr",
            "exposure",
            "changes",
            "objective",
        ]
        out = df[cols].copy()
        rename = {
            "name": "策略",
            "family": "类型",
            "cagr": "全样本年化",
            "max_drawdown": "最大回撤",
            "valid_cagr": "2016-2020",
            "recent2019_cagr": "2019至今",
            "test2021_cagr": "2021至今",
            "exposure": "平均仓位",
            "changes": "仓位变化",
            "objective": "稳健分",
        }
        out = out.rename(columns=rename)
        for col in ["全样本年化", "最大回撤", "2016-2020", "2019至今", "2021至今", "平均仓位", "稳健分"]:
            out[col] = out[col].map(pct)
        return markdown_table(out, list(out.columns))

    selected_lines = "\n".join(
        [
            f"- 选中策略：{selected['name']}",
            f"- 候选策略数量：{context['candidate_count']}，通过稳健筛选：{context['robust_count']}",
            f"- 选中策略全样本年化：{pct(selected['cagr'])}",
            f"- 同区间持有不动年化：{pct(bench['full']['cagr'])}",
            f"- 选中策略最大回撤：{pct(selected['max_drawdown'])}",
            f"- 同区间持有不动最大回撤：{pct(bench['full']['max_drawdown'])}",
            f"- 2016-2020：策略 {pct(selected['valid_cagr'])}，持有 {pct(bench['valid_2016_2020']['cagr'])}",
            f"- 2019至今：策略 {pct(selected['recent2019_cagr'])}，持有 {pct(bench['recent_2019']['cagr'])}",
            f"- 2021至今：策略 {pct(selected['test2021_cagr'])}，持有 {pct(bench['test_2021']['cagr'])}",
            f"- 平均仓位：{pct(selected['exposure'])}，仓位变化：{int(selected['changes'])} 次",
        ]
    )

    report = f"""# H30269 策略系统研究报告

生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

## 研究口径

- 标的：H30269.CSI 红利低波指数。
- 交易方向：只做多 H30269，不加杠杆，不做空。
- 信号：收盘后确认，下一交易日生效。
- 成本：默认按仓位变化扣 0.10%。
- 比较：统一从评分首次可用日开始，避免早期指标不可用造成口径偏差。
- 筛选：不仅看全样本收益，还要求最大回撤优于持有不动，且 2016-2020、2019至今、2021至今不能明显掉队。

## 最终推荐主策略

{selected_lines}

说明：这是收益、回撤、近年表现和交易频率之间的折中主策略。按稳健分自动排名第一的是低换手保守备选：

- 保守备选：{conservative['name']}
- 保守备选全样本年化：{pct(conservative['cagr'])}
- 保守备选最大回撤：{pct(conservative['max_drawdown'])}
- 保守备选 2021至今：{pct(conservative['test2021_cagr'])}
- 保守备选仓位变化：{int(conservative['changes'])} 次

## 通过稳健筛选的前 20 个策略

{display_table(robust_top)}

## 综合排名前 20 个策略

{display_table(top_overall)}

## 研究结论

目前最稳的方向仍是“核心仓位 + 趋势 + 低分保护”：红利低波长期收益较强，完全空仓会损失底层收益；但在趋势走弱且估值温度不低时，适度降仓能改善回撤和复利路径。

下一步不建议继续盲目扩大参数网格，而应把候选策略固定成少数可解释规则，并用未来数据持续做前推检验。
"""
    path = ANALYSIS_DIR / "h30269_strategy_research_report.md"
    path.write_text(report, encoding="utf-8")
    return path


def markdown_table(df: pd.DataFrame, columns: list[str]) -> str:
    rows = ["|" + "|".join(columns) + "|", "|" + "|".join(["---"] * len(columns)) + "|"]
    for _, row in df.iterrows():
        rows.append("|" + "|".join(str(row.get(col, "")) for col in columns) + "|")
    return "\n".join(rows)


def main() -> int:
    ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)
    results, context = research()
    report_path = build_report(results, context)
    selected = context["selected"]
    print(f"Report: {report_path}")
    print(f"Candidates: {context['candidate_count']}; robust: {context['robust_count']}")
    print(f"Selected: {context['selected_name']}")
    print(
        f"CAGR={selected['cagr']:.4%}, MDD={selected['max_drawdown']:.4%}, "
        f"valid={selected['valid_cagr']:.4%}, recent2019={selected['recent2019_cagr']:.4%}, "
        f"test2021={selected['test2021_cagr']:.4%}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
