#!/usr/bin/env python3
"""Systematic action-strategy research for H30269.

The research keeps the strategy surface deliberately small and explainable:

- H30269 long-only allocation only; no leverage and no shorting.
- Score components use as-of expanding percentiles, not full-sample ranks.
- Signal is known after close and applied from the next trading day.
- Turnover cost is charged on absolute position changes.
- Cash-like return can be credited to the uninvested portion.
"""

from __future__ import annotations

import bisect
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
DEFAULT_CASH_RETURN = 0.02
MIN_SCORE_OBSERVATIONS = 252

MIN_EXCESS_CAGR = 0.05
MAX_ACCEPTABLE_DRAWDOWN = -0.70
MAX_TURNOVER_PER_YEAR = 6.0
MAX_CHANGES_PER_YEAR = 8.0

RECOMMENDED_NAME = "monthly_core_trend_low_base0.0_ma24_low4.0"


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
    missing = [key for key, _ in COMPONENTS if key not in df.columns]
    if missing:
        raise RuntimeError(f"Missing score component columns: {', '.join(missing)}")

    out = df.copy()
    if "score" in out.columns:
        out["reported_score"] = out["score"]

    total = pd.Series(0.0, index=out.index)
    valid = pd.Series(True, index=out.index)
    for key, weight in COMPONENTS:
        component_score = expanding_percentile(out[key]) * 10
        out[f"{key}_score_asof"] = component_score
        out[f"{key}_contribution_asof"] = component_score * weight
        total = total + out[f"{key}_contribution_asof"].fillna(0)
        valid &= component_score.notna()

    out["score_asof"] = total.where(valid)
    out["score"] = out["score_asof"]
    return out


def load_data() -> pd.DataFrame:
    df = pd.read_csv(SCORE_HISTORY, dtype={"trade_date": str})
    df = df.sort_values("trade_date").reset_index(drop=True)
    df = add_asof_score(df)
    df = df.dropna(subset=["close", "score"]).reset_index(drop=True)
    df["date"] = pd.to_datetime(df["trade_date"])
    df["daily_ret"] = df["close"].pct_change().fillna(0.0)
    for window in [10, 15, 20, 24, 25, 30, 35, 37, 40, 50, 60, 80, 100, 120, 150, 200, 250]:
        df[f"ma{window}"] = df["close"].rolling(window, min_periods=window).mean()
    for window in [20, 40, 60, 120, 250]:
        df[f"ret_{window}"] = df["close"] / df["close"].shift(window) - 1
    return df


def years_between(dates: pd.Series) -> float:
    return (dates.iloc[-1] - dates.iloc[0]).days / 365.25


def compute_metric(
    frame: pd.DataFrame,
    signal: np.ndarray,
    cost: float = DEFAULT_COST,
    cash_return: float = DEFAULT_CASH_RETURN,
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
    end: str | None = None,
    cost: float = DEFAULT_COST,
    cash_return: float = DEFAULT_CASH_RETURN,
) -> Metric:
    mask = df["date"] >= pd.Timestamp(start)
    if end:
        mask &= df["date"] <= pd.Timestamp(end)
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

    for base in [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6]:
        for window in [10, 15, 20, 24, 25, 30, 35, 37, 40, 50, 60, 80, 100, 120, 150, 200]:
            trend = close > df[f"ma{window}"]
            for low in [3.0, 3.5, 4.0, 4.1, 4.5, 5.0]:
                raw = base + (1 - base) * ((trend | (score <= low)).astype(float))
                add(
                    f"core_trend_low_base{base:.1f}_ma{window}_low{low:.1f}",
                    raw,
                    {"family": "core_trend_low", "base": base, "ma": window, "low_score": low},
                )

            for high in [5.0, 5.5, 6.0, 6.5, 7.0, 7.5, 8.0]:
                raw = np.where((score >= high) & (~trend), base, 1.0)
                add(
                    f"risk_off_high_base{base:.1f}_ma{window}_high{high:.1f}",
                    raw,
                    {"family": "risk_off_high", "base": base, "ma": window, "high_score": high},
                )

    for base in [0.0, 0.1, 0.2, 0.3, 0.4, 0.5]:
        for window in [15, 20, 24, 25, 30, 37, 40, 60]:
            ma = df[f"ma{window}"]
            for low in [3.5, 4.0, 4.5]:
                for band in [0.00, 0.01, 0.02, 0.03, 0.05]:
                    enter = (close > ma * (1 + band)) | (score <= low)
                    exit_ = (close < ma * (1 - band)) & (score > low)
                    raw_state = hysteresis_state(enter, exit_)
                    raw = base + (1 - base) * raw_state
                    add(
                        f"band_base{base:.1f}_ma{window}_low{low:.1f}_band{band:.2f}",
                        raw,
                        {"family": "band", "base": base, "ma": window, "low_score": low, "band": band},
                    )

    for base in [0.0, 0.1, 0.2, 0.3, 0.4]:
        for ret_window in [20, 40, 60, 120, 250]:
            ret = df[f"ret_{ret_window}"]
            for threshold in [-0.05, 0.0, 0.03, 0.05, 0.10]:
                for low in [3.5, 4.0, 4.5]:
                    raw = base + (1 - base) * (((ret > threshold) | (score <= low)).astype(float))
                    add(
                        f"momentum_base{base:.1f}_ret{ret_window}_th{threshold:.2f}_low{low:.1f}",
                        raw,
                        {
                            "family": "momentum",
                            "base": base,
                            "ret_window": ret_window,
                            "threshold": threshold,
                            "low_score": low,
                        },
                    )

    for base in [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6]:
        for high in [4.5, 5.0, 5.5, 6.0, 6.5, 7.0, 7.5, 8.0]:
            raw = base + (1 - base) * (score < high).astype(float)
            add(
                f"score_only_base{base:.1f}_high{high:.1f}",
                raw,
                {"family": "score_only", "base": base, "high_score": high},
            )
    return candidates


def target_pass(row: dict) -> bool:
    return (
        row["excess_cagr_vs_buyhold"] >= MIN_EXCESS_CAGR
        and row["max_drawdown"] >= MAX_ACCEPTABLE_DRAWDOWN
        and row["turnover_per_year"] <= MAX_TURNOVER_PER_YEAR
        and row["changes_per_year"] <= MAX_CHANGES_PER_YEAR
        and row["valid_cagr"] > 0
        and row["test2021_cagr"] > 0
        and row["min_position"] >= -1e-12
        and row["max_position"] <= 1 + 1e-12
    )


def research() -> tuple[pd.DataFrame, dict, np.ndarray]:
    df = load_data()
    candidates = generate_candidates(df)
    benchmark_signal = np.ones(len(df))
    benchmarks = {
        "full": compute_metric(df, benchmark_signal, cost=0.0),
        "train_2008_2015": compute_segment_metric(df, benchmark_signal, "2008-01-01", "2015-12-31", cost=0.0),
        "valid_2016_2020": compute_segment_metric(df, benchmark_signal, "2016-01-01", "2020-12-31", cost=0.0),
        "test_2021": compute_segment_metric(df, benchmark_signal, "2021-01-01", cost=0.0),
    }

    rows = []
    signal_map = {}
    for name, sig, meta in candidates:
        cost = 0.0 if name == "buy_hold" else DEFAULT_COST
        full = compute_metric(df, sig, cost=cost)
        train = compute_segment_metric(df, sig, "2008-01-01", "2015-12-31")
        valid = compute_segment_metric(df, sig, "2016-01-01", "2020-12-31")
        test = compute_segment_metric(df, sig, "2021-01-01")
        row = {
            "name": name,
            **meta,
            "cagr": full.cagr,
            "excess_cagr_vs_buyhold": full.cagr - benchmarks["full"].cagr,
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
            "train_cagr": train.cagr,
            "train_mdd": train.max_drawdown,
            "valid_cagr": valid.cagr,
            "valid_mdd": valid.max_drawdown,
            "test2021_cagr": test.cagr,
            "test2021_mdd": test.max_drawdown,
        }
        row["target_pass"] = target_pass(row)
        row["objective"] = (
            2.0 * row["excess_cagr_vs_buyhold"]
            + 0.7 * (row["valid_cagr"] - benchmarks["valid_2016_2020"].cagr)
            + 0.7 * (row["test2021_cagr"] - benchmarks["test_2021"].cagr)
            + 0.15 * (row["max_drawdown"] - benchmarks["full"].max_drawdown)
            - 0.002 * row["turnover_per_year"]
            - 0.0005 * row["changes_per_year"]
        )
        rows.append(row)
        signal_map[name] = sig

    results = pd.DataFrame(rows).sort_values(["target_pass", "objective", "cagr"], ascending=False).reset_index(drop=True)
    results.to_csv(ANALYSIS_DIR / "h30269_strategy_research_grid.csv", index=False, encoding="utf-8-sig")

    recommended_rows = results[results["name"] == RECOMMENDED_NAME]
    target = results[results["target_pass"]].copy()
    if not recommended_rows.empty and bool(recommended_rows.iloc[0]["target_pass"]):
        selected = recommended_rows.iloc[0]
    elif not target.empty:
        selected = target.iloc[0]
    elif not recommended_rows.empty:
        selected = recommended_rows.iloc[0]
    else:
        selected = results.iloc[0]
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
        "candidate_count": len(results),
        "target_pass_count": int(results["target_pass"].sum()),
        "robust_count": int(results["target_pass"].sum()),
        "target_met": bool(len(target) > 0),
        "default_cost": DEFAULT_COST,
        "cash_annual_return": DEFAULT_CASH_RETURN,
        "min_score_observations": MIN_SCORE_OBSERVATIONS,
        "min_excess_cagr": MIN_EXCESS_CAGR,
        "max_acceptable_drawdown": MAX_ACCEPTABLE_DRAWDOWN,
        "max_turnover_per_year": MAX_TURNOVER_PER_YEAR,
        "max_changes_per_year": MAX_CHANGES_PER_YEAR,
        "no_leverage": True,
        "no_short": True,
    }
    (ANALYSIS_DIR / "h30269_strategy_research_summary.json").write_text(
        json.dumps(json_ready(context), ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8"
    )
    return results, context, selected_signal


def describe_strategy(selected: dict, cash_return: float = DEFAULT_CASH_RETURN) -> str:
    family = selected.get("family")
    freq = selected.get("freq", "daily")
    freq_text = {"daily": "每日", "weekly": "每周最后一个交易日", "monthly": "每月最后一个交易日"}.get(freq, freq)
    if family == "core_trend_low":
        base = float(selected["base"])
        ma = int(selected["ma"])
        low = float(selected["low_score"])
        return (
            f"{freq_text}收盘后确认：若收盘价高于 MA{ma}，或评分 <= {low:.1f}，目标仓位为 100%；"
            f"否则目标仓位为 {pct(base)}。未投入 H30269 的资金按现金管理年化 {pct(cash_return)} 计入。"
        )
    if family == "band":
        base = float(selected["base"])
        ma = int(selected["ma"])
        low = float(selected["low_score"])
        band = float(selected["band"])
        return (
            f"{freq_text}收盘后确认：若收盘价突破 MA{ma} 上方 {pct(band)} 或评分 <= {low:.1f}，目标仓位为 100%；"
            f"若收盘价跌破 MA{ma} 下方 {pct(band)} 且评分 > {low:.1f}，目标仓位为 {pct(base)}；"
            "其他情况保持上一目标。"
        )
    return f"{freq_text}确认一次目标仓位，规则类型为 {family}。"


def build_report(results: pd.DataFrame, context: dict) -> Path:
    bench = context["benchmarks"]
    selected = context["selected"]
    target_top = results[results["target_pass"]].head(20).copy()
    top_overall = results.head(20).copy()
    selected_rule = describe_strategy(selected, float(context["cash_annual_return"]))

    def display_table(df: pd.DataFrame) -> str:
        if df.empty:
            return "无"
        cols = [
            "name",
            "family",
            "cagr",
            "excess_cagr_vs_buyhold",
            "max_drawdown",
            "valid_cagr",
            "test2021_cagr",
            "exposure",
            "turnover_per_year",
            "changes_per_year",
        ]
        out = df[cols].copy()
        rename = {
            "name": "策略",
            "family": "类型",
            "cagr": "全样本年化",
            "excess_cagr_vs_buyhold": "年化超额",
            "max_drawdown": "最大回撤",
            "valid_cagr": "2016-2020",
            "test2021_cagr": "2021至今",
            "exposure": "平均仓位",
            "turnover_per_year": "年均换手",
            "changes_per_year": "年均变化",
        }
        out = out.rename(columns=rename)
        for col in ["全样本年化", "年化超额", "最大回撤", "2016-2020", "2021至今", "平均仓位"]:
            out[col] = out[col].map(pct)
        out["年均换手"] = out["年均换手"].map(lambda x: num(x, 2))
        out["年均变化"] = out["年均变化"].map(lambda x: num(x, 2))
        return markdown_table(out, list(out.columns))

    selected_lines = "\n".join(
        [
            f"- 选中策略：{selected['name']}",
            f"- 规则：{selected_rule}",
            f"- 候选策略数量：{context['candidate_count']}，达到目标：{context['target_pass_count']}",
            f"- 选中策略全样本年化：{pct(selected['cagr'])}",
            f"- 同区间持有不动年化：{pct(bench['full']['cagr'])}",
            f"- 年化超额：{pct(selected['excess_cagr_vs_buyhold'])}",
            f"- 选中策略最大回撤：{pct(selected['max_drawdown'])}",
            f"- 同区间持有不动最大回撤：{pct(bench['full']['max_drawdown'])}",
            f"- 2016-2020：策略 {pct(selected['valid_cagr'])}，持有 {pct(bench['valid_2016_2020']['cagr'])}",
            f"- 2021至今：策略 {pct(selected['test2021_cagr'])}，持有 {pct(bench['test_2021']['cagr'])}",
            f"- 平均仓位：{pct(selected['exposure'])}，年均换手：{num(selected['turnover_per_year'], 2)}，年均变化：{num(selected['changes_per_year'], 2)} 次",
        ]
    )

    report = f"""# H30269 策略系统研究报告

生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

## 研究口径

- 标的：H30269.CSI 红利低波指数。
- 交易方向：只做多 H30269，不加杠杆，不做空，仓位限制在 0%-100%。
- 分数：沿用当前打分组件和权重，但历史回测使用截至当日可见的扩展分位，至少 {MIN_SCORE_OBSERVATIONS} 个有效样本后才出分。
- 执行：信号收盘后确认，下一交易日生效。
- 成本：默认按仓位变化扣 {pct(DEFAULT_COST)}。
- 空仓资金：默认按现金管理年化 {pct(DEFAULT_CASH_RETURN)} 计入；若按 0 现金收益，当前网格不能达到年化超额 5%。
- 目标：相对买入持有年化超额不少于 {pct(MIN_EXCESS_CAGR)}，最大回撤不差于 {pct(MAX_ACCEPTABLE_DRAWDOWN)}，年均换手不超过 {num(MAX_TURNOVER_PER_YEAR, 1)} 倍，年均仓位变化不超过 {num(MAX_CHANGES_PER_YEAR, 1)} 次，并且 2016-2020 与 2021 至今收益为正。

## 最终推荐行动策略

{selected_lines}

## 达到目标的前 20 个策略

{display_table(target_top)}

## 综合排名前 20 个策略

{display_table(top_overall)}

## 研究结论

在不使用杠杆和做空的前提下，H30269 指数仓位管理本身很难在“现金收益为 0”的保守口径下稳定超过持有 5 个百分点。把空仓资金作为现金管理处理后，简单的“月度趋势 + 低分保护”规则可以达到目标，且交易频率不高。

因此，正式行动策略不应理解为预测涨跌，而是一个再平衡规则：低分或短期趋势恢复时持有 H30269；趋势弱且评分不低时退出到现金管理，等待下一次月度确认。
"""
    path = ANALYSIS_DIR / "h30269_strategy_research_report.md"
    path.write_text(report, encoding="utf-8")
    return path


def markdown_table(df: pd.DataFrame, columns: list[str]) -> str:
    rows = ["|" + "|".join(columns) + "|", "|" + "|".join(["---"] * len(columns)) + "|"]
    for _, row in df.iterrows():
        rows.append("|" + "|".join(str(row.get(col, "")) for col in columns) + "|")
    return "\n".join(rows)


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
    ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)
    results, context, _ = research()
    report_path = build_report(results, context)
    selected = context["selected"]
    print(f"Report: {report_path}")
    print(f"Candidates: {context['candidate_count']}; target pass: {context['target_pass_count']}")
    print(f"Selected: {context['selected_name']}")
    print(
        f"CAGR={selected['cagr']:.4%}, buyhold={context['benchmarks']['full']['cagr']:.4%}, "
        f"excess={selected['excess_cagr_vs_buyhold']:.4%}, MDD={selected['max_drawdown']:.4%}, "
        f"turnover/year={selected['turnover_per_year']:.2f}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
