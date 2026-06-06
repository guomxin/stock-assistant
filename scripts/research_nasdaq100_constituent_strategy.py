#!/usr/bin/env python3
"""Research long-only Nasdaq-100 constituent rotation strategies.

This script intentionally uses only non-negative weights summing to at most 100%.
When Wikipedia's change table is available it reconstructs an approximate historical
membership set.  Delisted/acquired tickers may still be missing from Yahoo, so the
backtest remains an approximation rather than a perfect point-in-time index replay.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd


BASE_DIR = Path(__file__).resolve().parents[1]
CONSTITUENT_DIR = BASE_DIR / "data" / "raw" / "nasdaq100_constituents"
RAW_US_STOCK_DIR = BASE_DIR / "data" / "raw" / "us_stock_daily"
RAW_INDEX_DIR = BASE_DIR / "data" / "raw" / "index_daily"
ANALYSIS_DIR = BASE_DIR / "analysis" / "nasdaq100"

DEFAULT_START_DATE = "20050101"
DEFAULT_COST = 0.0005
MIN_EXCESS_CAGR = 0.05
MAX_ACCEPTABLE_DRAWDOWN = -0.70
MAX_TURNOVER_PER_YEAR = 8.0
MAX_REBALANCES_PER_YEAR = 12.5


@dataclass(frozen=True)
class Metric:
    cagr: float
    total_return: float
    max_drawdown: float
    annual_vol: float
    sharpe: float
    exposure: float
    turnover: float
    turnover_per_year: float
    changes: int
    changes_per_year: float
    years: float


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Research Nasdaq-100 constituent long-only rotation strategies.")
    parser.add_argument("--start-date", default=DEFAULT_START_DATE)
    parser.add_argument("--end-date", default=datetime.now().strftime("%Y%m%d"))
    parser.add_argument("--cost", type=float, default=DEFAULT_COST)
    parser.add_argument(
        "--membership-mode",
        choices=["reconstructed", "current"],
        default="reconstructed",
        help="Use Wikipedia change table reconstruction or the latest constituent list only.",
    )
    return parser.parse_args()


def safe_code(code: str) -> str:
    return code.replace("^", "").replace(".", "_").replace("-", "_").replace("/", "_")


def latest_file(directory: Path, pattern: str) -> Path:
    files = sorted(directory.glob(pattern))
    if not files:
        raise FileNotFoundError(f"No files matching {directory / pattern}")
    return files[-1]


def price_cache_path(symbol: str, start_date: str, end_date: str) -> Path:
    return RAW_US_STOCK_DIR / f"{safe_code(symbol)}_YAHOO_{start_date}_{end_date}.parquet"


def load_constituents() -> pd.DataFrame:
    path = latest_file(CONSTITUENT_DIR, "nasdaq100_constituents_*.parquet")
    df = pd.read_parquet(path).sort_values("yahoo_symbol").reset_index(drop=True)
    df["constituent_cache_path"] = str(path)
    return df


def load_changes() -> pd.DataFrame:
    path = latest_file(CONSTITUENT_DIR, "nasdaq100_changes_*.parquet")
    df = pd.read_parquet(path).sort_values("trade_date", ascending=False).reset_index(drop=True)
    df["change_cache_path"] = str(path)
    return df


def universe_symbols(constituents: pd.DataFrame, changes: pd.DataFrame | None, membership_mode: str) -> list[str]:
    symbols = set(constituents["yahoo_symbol"].astype(str))
    if membership_mode == "reconstructed" and changes is not None:
        for column in ["added_yahoo_symbol", "removed_yahoo_symbol"]:
            symbols.update(s for s in changes[column].astype(str) if s and s.lower() != "nan")
    return sorted(symbols)


def load_prices(symbols: list[str], start_date: str, end_date: str) -> tuple[pd.DataFrame, list[str]]:
    frames = []
    missing = []
    for symbol in symbols:
        path = price_cache_path(symbol, start_date, end_date)
        if not path.exists():
            missing.append(symbol)
            continue
        df = pd.read_parquet(path)
        if df.empty or "adj_close" not in df.columns:
            missing.append(symbol)
            continue
        keep = df[["trade_date", "adj_close"]].copy()
        keep["trade_date"] = keep["trade_date"].astype(str)
        keep = keep.rename(columns={"adj_close": symbol})
        frames.append(keep)
    if missing and not frames:
        raise FileNotFoundError(f"Missing cached prices for: {', '.join(missing)}")
    prices = frames[0]
    for frame in frames[1:]:
        prices = prices.merge(frame, on="trade_date", how="outer")
    return prices.sort_values("trade_date").set_index("trade_date"), sorted(missing)


def build_membership(
    dates: pd.Index,
    price_columns: pd.Index,
    constituents: pd.DataFrame,
    changes: pd.DataFrame | None,
    membership_mode: str,
) -> pd.DataFrame:
    columns = list(price_columns)
    if membership_mode == "current" or changes is None:
        return pd.DataFrame(True, index=dates, columns=columns)

    current = set(constituents["yahoo_symbol"].astype(str)).intersection(columns)
    change_rows = changes[["trade_date", "added_yahoo_symbol", "removed_yahoo_symbol"]].copy()
    change_rows["added_yahoo_symbol"] = change_rows["added_yahoo_symbol"].fillna("").astype(str)
    change_rows["removed_yahoo_symbol"] = change_rows["removed_yahoo_symbol"].fillna("").astype(str)
    rows = []
    for date in dates:
        active = set(current)
        future_changes = change_rows[change_rows["trade_date"] > str(date)]
        for _, change in future_changes.iterrows():
            added = change["added_yahoo_symbol"]
            removed = change["removed_yahoo_symbol"]
            if added:
                active.discard(added)
            if removed and removed in columns:
                active.add(removed)
        rows.append([symbol in active for symbol in columns])
    return pd.DataFrame(rows, index=dates, columns=columns)


def load_benchmark(start_date: str, end_date: str) -> pd.Series:
    exact = RAW_INDEX_DIR / f"NDX_YAHOO_{start_date}_{end_date}.parquet"
    path = exact if exact.exists() else latest_file(RAW_INDEX_DIR, "NDX_YAHOO_*.parquet")
    df = pd.read_parquet(path).sort_values("trade_date")
    df["trade_date"] = df["trade_date"].astype(str)
    return df.set_index("trade_date")["close"].rename("NDX")


def years_between(index: pd.Index) -> float:
    dates = pd.to_datetime(pd.Series(index.astype(str)))
    return max((dates.iloc[-1] - dates.iloc[0]).days / 365.25, 1 / 365.25)


def metric_from_returns(ret: pd.Series, exposure: pd.Series | None = None, turnover: pd.Series | None = None) -> Metric:
    ret = ret.fillna(0.0)
    nav = (1.0 + ret).cumprod()
    years = years_between(ret.index)
    cagr = nav.iloc[-1] ** (1 / years) - 1
    dd = nav / nav.cummax() - 1
    vol = ret.std() * math.sqrt(252)
    exposure_value = float(exposure.mean()) if exposure is not None else 1.0
    turnover_sum = float(turnover.sum()) if turnover is not None else 0.0
    changes = int((turnover.fillna(0.0) > 1e-12).sum()) if turnover is not None else 0
    return Metric(
        cagr=float(cagr),
        total_return=float(nav.iloc[-1] - 1),
        max_drawdown=float(dd.min()),
        annual_vol=float(vol),
        sharpe=float(cagr / vol) if vol else np.nan,
        exposure=exposure_value,
        turnover=turnover_sum,
        turnover_per_year=float(turnover_sum / years),
        changes=changes,
        changes_per_year=float(changes / years),
        years=float(years),
    )


def pct_rank(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.rank(axis=1, pct=True)


def build_feature_data(prices: pd.DataFrame) -> dict[str, pd.DataFrame]:
    returns = prices.pct_change()
    return {
        "ret_63": prices / prices.shift(63) - 1,
        "ret_126": prices / prices.shift(126) - 1,
        "ret_252": prices / prices.shift(252) - 1,
        "ret_252_skip21": prices.shift(21) / prices.shift(252) - 1,
        "vol_126": returns.rolling(126, min_periods=63).std() * math.sqrt(252),
        "dd_252": prices / prices.rolling(252, min_periods=126).max() - 1,
        "ma200_dev": prices / prices.rolling(200, min_periods=120).mean() - 1,
    }


def score_frame(features: dict[str, pd.DataFrame], family: str) -> pd.DataFrame:
    mom12 = pct_rank(features["ret_252_skip21"])
    mom6 = pct_rank(features["ret_126"])
    mom3 = pct_rank(features["ret_63"])
    low_vol = 1.0 - pct_rank(features["vol_126"])
    drawdown_repair = pct_rank(features["dd_252"])
    trend = pct_rank(features["ma200_dev"])

    if family == "momentum":
        return mom12 + 0.75 * mom6 + 0.25 * mom3
    if family == "momentum_lowvol":
        return mom12 + 0.5 * mom6 + 0.5 * low_vol + 0.25 * drawdown_repair
    if family == "lowvol_trend":
        return 0.75 * low_vol + 0.5 * mom12 + 0.5 * trend + 0.25 * drawdown_repair
    if family == "balanced":
        return mom12 + 0.5 * mom6 + 0.5 * trend + 0.5 * low_vol + 0.5 * drawdown_repair
    raise ValueError(family)


def rebalance_dates(index: pd.Index, freq: str) -> pd.Index:
    dates = pd.to_datetime(index.astype(str))
    if freq == "monthly":
        keys = dates.to_period("M")
    elif freq == "quarterly":
        keys = dates.to_period("Q")
    else:
        raise ValueError(freq)
    return pd.DataFrame({"key": keys}, index=index).groupby("key").tail(1).index


def build_weights(prices: pd.DataFrame, membership: pd.DataFrame, meta: dict) -> pd.DataFrame:
    features = build_feature_data(prices)
    scores = score_frame(features, str(meta["family"]))
    dates = prices.index
    weights = pd.DataFrame(np.nan, index=dates, columns=prices.columns, dtype=float)
    rebal_idx = rebalance_dates(dates, str(meta["freq"]))
    top_n = int(meta["top_n"])
    min_names = max(5, min(top_n, top_n // 3))

    for date in rebal_idx:
        score = scores.loc[date].copy()
        eligible = score.notna() & features["ret_252_skip21"].loc[date].notna() & features["vol_126"].loc[date].notna()
        eligible &= membership.loc[date].reindex(prices.columns).fillna(False)
        if bool(meta["require_positive_momentum"]):
            eligible &= features["ret_252_skip21"].loc[date] > 0
        if bool(meta["require_ma200"]):
            eligible &= features["ma200_dev"].loc[date] > 0
        ranked = score[eligible].sort_values(ascending=False).head(top_n)
        row = pd.Series(0.0, index=prices.columns)
        if len(ranked) >= min_names:
            row.loc[ranked.index] = 1.0 / len(ranked)
        weights.loc[date] = row
    return weights.ffill().fillna(0.0)


def backtest(
    prices: pd.DataFrame,
    benchmark: pd.Series,
    membership: pd.DataFrame,
    meta: dict,
    cost: float,
) -> tuple[pd.DataFrame, Metric]:
    target = build_weights(prices, membership, meta)
    stock_ret = prices.pct_change().reindex(target.index).fillna(0.0)
    position = target.shift(1).fillna(0.0)
    turnover = position.sub(position.shift(1).fillna(0.0)).abs().sum(axis=1)
    portfolio_ret = (position * stock_ret).sum(axis=1) - turnover * cost
    exposure = position.sum(axis=1)
    bench_ret = benchmark.reindex(target.index).ffill().pct_change().fillna(0.0)
    nav = pd.DataFrame(
        {
            "strategy_ret": portfolio_ret,
            "benchmark_ret": bench_ret,
            "strategy_nav": (1 + portfolio_ret).cumprod(),
            "benchmark_nav": (1 + bench_ret).cumprod(),
            "exposure": exposure,
            "turnover": turnover,
        },
        index=target.index,
    ).reset_index(names="trade_date")
    return nav, metric_from_returns(portfolio_ret, exposure, turnover)


def segment_metric(nav: pd.DataFrame, column: str, start: str, end: str | None) -> Metric:
    df = nav.copy()
    mask = df["trade_date"] >= start.replace("-", "")
    if end:
        mask &= df["trade_date"] <= end.replace("-", "")
    sub = df.loc[mask]
    return metric_from_returns(pd.Series(sub[column].to_numpy(), index=sub["trade_date"]))


def fmt_pct(value: float | None) -> str:
    if value is None or not np.isfinite(value):
        return "-"
    return f"{value * 100:.2f}%"


def fmt_num(value: float | None, digits: int = 2) -> str:
    if value is None or not np.isfinite(value):
        return "-"
    return f"{value:.{digits}f}"


def json_ready(value):
    if isinstance(value, dict):
        return {str(k): json_ready(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_ready(v) for v in value]
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, (np.floating, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    return value


def markdown_table(df: pd.DataFrame, columns: list[str]) -> str:
    rows = ["|" + "|".join(columns) + "|", "|" + "|".join(["---"] * len(columns)) + "|"]
    for _, row in df.iterrows():
        rows.append("|" + "|".join(str(row.get(col, "")) for col in columns) + "|")
    return "\n".join(rows)


def candidate_grid() -> list[dict]:
    rows = []
    for family in ["momentum", "momentum_lowvol", "lowvol_trend", "balanced"]:
        for freq in ["monthly", "quarterly"]:
            for top_n in [10, 15, 20, 25, 30, 40]:
                for require_positive_momentum in [True, False]:
                    for require_ma200 in [True, False]:
                        rows.append(
                            {
                                "family": family,
                                "freq": freq,
                                "top_n": top_n,
                                "require_positive_momentum": require_positive_momentum,
                                "require_ma200": require_ma200,
                            }
                        )
    return rows


def run_research(
    prices: pd.DataFrame,
    benchmark: pd.Series,
    membership: pd.DataFrame,
    cost: float,
    data_meta: dict,
) -> tuple[pd.DataFrame, dict, pd.DataFrame, pd.DataFrame]:
    rows = []
    nav_map = {}
    bench_metric = metric_from_returns(benchmark.reindex(prices.index).ffill().pct_change().fillna(0.0))
    for meta in candidate_grid():
        name = (
            f"{meta['freq']}_{meta['family']}_top{meta['top_n']}"
            f"_mom{int(meta['require_positive_momentum'])}_ma{int(meta['require_ma200'])}"
        )
        nav, full = backtest(prices, benchmark, membership, meta, cost)
        train = segment_metric(nav, "strategy_ret", "2007-01-01", "2015-12-31")
        valid = segment_metric(nav, "strategy_ret", "2016-01-01", "2021-12-31")
        test = segment_metric(nav, "strategy_ret", "2022-01-01", None)
        rows.append(
            {
                "name": name,
                **meta,
                "cagr": full.cagr,
                "excess_cagr_vs_buyhold": full.cagr - bench_metric.cagr,
                "total_return": full.total_return,
                "max_drawdown": full.max_drawdown,
                "annual_vol": full.annual_vol,
                "sharpe": full.sharpe,
                "exposure": full.exposure,
                "turnover": full.turnover,
                "turnover_per_year": full.turnover_per_year,
                "changes": full.changes,
                "changes_per_year": full.changes_per_year,
                "train_cagr": train.cagr,
                "valid_cagr": valid.cagr,
                "test2022_cagr": test.cagr,
                "valid_mdd": valid.max_drawdown,
                "test2022_mdd": test.max_drawdown,
            }
        )
        nav_map[name] = nav

    grid = pd.DataFrame(rows)
    constrained = grid[
        (grid["max_drawdown"] >= MAX_ACCEPTABLE_DRAWDOWN)
        & (grid["turnover_per_year"] <= MAX_TURNOVER_PER_YEAR)
        & (grid["changes_per_year"] <= MAX_REBALANCES_PER_YEAR)
        & (grid["valid_cagr"] > 0)
        & (grid["test2022_cagr"] > 0)
    ].copy()
    target_pass = constrained[constrained["excess_cagr_vs_buyhold"] >= MIN_EXCESS_CAGR].copy()
    grid["constraint_pass"] = grid["name"].isin(set(constrained["name"]))
    grid["selection_pass"] = grid["name"].isin(set(target_pass["name"]))
    grid = grid.sort_values(
        ["selection_pass", "constraint_pass", "cagr", "sharpe", "max_drawdown", "turnover_per_year"],
        ascending=[False, False, False, False, False, True],
    ).reset_index(drop=True)
    if not target_pass.empty:
        selected = target_pass.sort_values(
            ["sharpe", "max_drawdown", "cagr", "turnover_per_year"],
            ascending=[False, False, False, True],
        ).iloc[0].to_dict()
    elif not constrained.empty:
        selected = constrained.sort_values(
            ["cagr", "sharpe", "max_drawdown", "turnover_per_year"],
            ascending=[False, False, False, True],
        ).iloc[0].to_dict()
    else:
        selected = grid.iloc[0].to_dict()
    selected_nav = nav_map[str(selected["name"])]
    summary = {
        "cost": cost,
        "min_excess_cagr": MIN_EXCESS_CAGR,
        "max_acceptable_drawdown": MAX_ACCEPTABLE_DRAWDOWN,
        "max_turnover_per_year": MAX_TURNOVER_PER_YEAR,
        "max_rebalances_per_year": MAX_REBALANCES_PER_YEAR,
        "selection_policy": "target_pass_then_sharpe_drawdown_cagr_turnover",
        "no_leverage": True,
        "no_short": True,
        **data_meta,
        "target_met": bool(not target_pass.empty),
        "benchmark": bench_metric.__dict__,
        "selected": selected,
        "target_pass_candidates": int(len(target_pass)),
        "constrained_candidates": int(len(constrained)),
        "total_candidates": int(len(grid)),
    }
    return grid, summary, selected_nav, build_weights(prices, membership, selected)


def describe_strategy(selected: dict) -> str:
    freq_text = {"monthly": "每月", "quarterly": "每季度"}.get(str(selected["freq"]), str(selected["freq"]))
    filters = []
    if bool(selected["require_positive_momentum"]):
        filters.append("近 12-1 月动量为正")
    if bool(selected["require_ma200"]):
        filters.append("价格在 MA200 之上")
    filter_text = "，并要求" + "且".join(filters) if filters else ""
    return (
        f"{freq_text}调仓一次；在当期纳指100成分池中按 `{selected['family']}` 分数排序{filter_text}，"
        f"等权持有前 {int(selected['top_n'])} 只股票；不满足持仓数量时保留现金。"
    )


def write_outputs(
    constituents: pd.DataFrame,
    prices: pd.DataFrame,
    grid: pd.DataFrame,
    summary: dict,
    selected_nav: pd.DataFrame,
    latest_weights: pd.DataFrame,
) -> Path:
    ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)
    grid_path = ANALYSIS_DIR / "nasdaq100_constituent_strategy_grid.csv"
    summary_path = ANALYSIS_DIR / "nasdaq100_constituent_strategy_summary.json"
    nav_path = ANALYSIS_DIR / "nasdaq100_constituent_strategy_nav.csv"
    weights_path = ANALYSIS_DIR / "nasdaq100_constituent_latest_weights.csv"
    grid.to_csv(grid_path, index=False, encoding="utf-8-sig")
    summary_path.write_text(json.dumps(json_ready(summary), ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    selected_nav.to_csv(nav_path, index=False, encoding="utf-8-sig")
    latest_weights.to_csv(weights_path, index=False, encoding="utf-8-sig")

    selected = summary["selected"]
    benchmark = summary["benchmark"]
    top = grid.head(15).copy()
    for col in ["cagr", "excess_cagr_vs_buyhold", "max_drawdown", "annual_vol", "test2022_cagr"]:
        top[f"{col}_fmt"] = top[col].map(fmt_pct)
    top["turnover_per_year_fmt"] = top["turnover_per_year"].map(lambda x: fmt_num(x, 2))
    top["changes_per_year_fmt"] = top["changes_per_year"].map(lambda x: fmt_num(x, 2))
    display_weights = latest_weights.head(20).copy()
    display_weights["weight_fmt"] = display_weights["weight"].map(fmt_pct)
    status = "通过" if summary["target_met"] else "未通过"
    if summary["membership_mode"] == "current":
        status = f"{status}（探索性，存在幸存者偏差）"

    report = f"""# 纳斯达克100成分股长-only轮动策略研究

生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

## 当前结论

- 成分模式：{summary['membership_mode']}
- 当前成分股数量：{summary['current_constituents']}
- 变更记录数量：{summary['change_rows']}
- 历史候选 ticker：{summary['universe_symbols']}，有价格覆盖：{summary['priced_symbols']}，缺失价格：{summary['missing_price_symbols']}
- 当期成分价格覆盖：平均 {fmt_pct(float(summary['active_price_coverage_avg']))}，10分位 {fmt_pct(float(summary['active_price_coverage_p10']))}，最低 {fmt_pct(float(summary['active_price_coverage_min']))}，最新 {fmt_pct(float(summary['active_price_coverage_latest']))}
- 数据来源：Wikipedia 纳指100当前成分与变更表，Yahoo Finance 复权日线
- 回测区间：{selected_nav['trade_date'].iloc[0]} 至 {selected_nav['trade_date'].iloc[-1]}
- 目标检验：**{status}**
- 选中策略：`{selected['name']}`
- 策略规则：{describe_strategy(selected)}
- 策略年化收益：{fmt_pct(float(selected['cagr']))}
- 纳斯达克100持有年化收益：{fmt_pct(float(benchmark['cagr']))}
- 年化超额：{fmt_pct(float(selected['excess_cagr_vs_buyhold']))}
- 策略最大回撤：{fmt_pct(float(selected['max_drawdown']))}
- 策略年均换手：{fmt_num(float(selected['turnover_per_year']), 2)} 倍
- 年均调仓发生次数：{fmt_num(float(selected['changes_per_year']), 2)}
- 目标筛选通过数量：{summary['target_pass_candidates']} / {summary['total_candidates']}

重要限制：`reconstructed` 模式用 Wikipedia 变更表反向重建历史成分池，已比“当前成分股回看历史”更接近真实，但仍不是官方点位成分数据库；部分退市或并购 ticker 无法从 Yahoo 取得价格，可能留下覆盖偏差。若 `membership_mode=current`，则存在明显幸存者偏差，只能视为探索性结果。

## 最新持仓建议

{markdown_table(display_weights, ['symbol', 'company', 'sector', 'weight_fmt'])}

## 搜索方法

候选策略均为不做空、不使用杠杆的等权多头组合，目标仓位不超过 100%。搜索的分数族包括：

- `momentum`：12-1 月动量、6 月动量和 3 月动量。
- `momentum_lowvol`：动量、低波动和一年回撤修复。
- `lowvol_trend`：低波动、12-1 月动量、趋势和回撤修复。
- `balanced`：动量、趋势、低波动和回撤修复的均衡组合。

筛选要求：年化超额不少于 {fmt_pct(MIN_EXCESS_CAGR)}，最大回撤不差于 {fmt_pct(MAX_ACCEPTABLE_DRAWDOWN)}，年均换手不超过 {fmt_num(MAX_TURNOVER_PER_YEAR, 2)} 倍，年均调仓发生次数不超过 {fmt_num(MAX_REBALANCES_PER_YEAR, 2)}，并且 2016-2021 与 2022 年后样本年化收益为正。

选中原则：先满足目标筛选，再优先选择夏普更高、回撤更小的候选，其次比较年化收益和换手率。

## 候选策略前十五

{markdown_table(top, ['name', 'cagr_fmt', 'excess_cagr_vs_buyhold_fmt', 'max_drawdown_fmt', 'annual_vol_fmt', 'test2022_cagr_fmt', 'turnover_per_year_fmt', 'changes_per_year_fmt'])}

## 后续工作

若要进一步提高严谨性，应接入 Nasdaq 官方或商业数据库的点位历史成分与公司行为数据，减少 Yahoo 对退市 ticker 覆盖不足造成的偏差。
"""
    report_path = ANALYSIS_DIR / "nasdaq100_constituent_strategy_report.md"
    report_path.write_text(report, encoding="utf-8")
    return report_path


def main() -> int:
    args = parse_args()
    constituents = load_constituents()
    changes = load_changes() if args.membership_mode == "reconstructed" else None
    symbols = universe_symbols(constituents, changes, args.membership_mode)
    prices, missing_symbols = load_prices(symbols, args.start_date, args.end_date)
    benchmark = load_benchmark(args.start_date, args.end_date)
    dates = benchmark.index.intersection(prices.index)
    prices = prices.reindex(dates).ffill()
    warmup_start = "20061229"
    if args.membership_mode == "reconstructed" and changes is not None:
        warmup_start = max(warmup_start, str(changes["trade_date"].min()))
    warmup_dates = dates[dates >= warmup_start]
    prices = prices.loc[warmup_dates]
    benchmark = benchmark.loc[warmup_dates]
    full_membership = build_membership(prices.index, pd.Index(symbols), constituents, changes, args.membership_mode)
    membership = build_membership(prices.index, prices.columns, constituents, changes, args.membership_mode)
    full_active = full_membership.sum(axis=1)
    priced_active = membership.sum(axis=1)
    active_coverage = priced_active / full_active.replace(0, np.nan)
    data_meta = {
        "membership_mode": args.membership_mode,
        "current_constituents": int(len(constituents)),
        "change_rows": int(len(changes)) if changes is not None else 0,
        "universe_symbols": int(len(symbols)),
        "priced_symbols": int(len(prices.columns)),
        "missing_price_symbols": int(len(missing_symbols)),
        "missing_price_symbol_list": missing_symbols,
        "active_members_avg": float(full_active.mean()),
        "priced_active_members_avg": float(priced_active.mean()),
        "active_price_coverage_min": float(active_coverage.min()),
        "active_price_coverage_avg": float(active_coverage.mean()),
        "active_price_coverage_median": float(active_coverage.median()),
        "active_price_coverage_p10": float(active_coverage.quantile(0.10)),
        "active_price_coverage_latest": float(active_coverage.iloc[-1]),
        "uses_current_constituents_only": args.membership_mode == "current",
        "uses_reconstructed_membership": args.membership_mode == "reconstructed",
        "coverage_bias_warning": bool(missing_symbols),
    }

    grid, summary, selected_nav, weights = run_research(prices, benchmark, membership, args.cost, data_meta)
    latest = weights.iloc[-1]
    latest_weights = latest[latest > 0].sort_values(ascending=False).reset_index()
    latest_weights.columns = ["symbol", "weight"]
    latest_weights = latest_weights.merge(
        constituents[["yahoo_symbol", "company", "sector"]].rename(columns={"yahoo_symbol": "symbol"}),
        on="symbol",
        how="left",
    )
    latest_weights = latest_weights[["symbol", "company", "sector", "weight"]]
    report_path = write_outputs(constituents, prices, grid, summary, selected_nav, latest_weights)

    selected = summary["selected"]
    benchmark_metric = summary["benchmark"]
    print(f"Report: {report_path}")
    print(f"Membership mode: {summary['membership_mode']}")
    print(f"Constituents: {len(constituents)} current; universe={len(symbols)}; priced={len(prices.columns)}; missing={len(missing_symbols)}")
    print(f"Target met: {summary['target_met']}")
    print(f"Selected strategy: {selected['name']}")
    print(f"Strategy CAGR: {selected['cagr']:.2%}; benchmark CAGR: {benchmark_metric['cagr']:.2%}")
    print(f"Excess CAGR: {selected['excess_cagr_vs_buyhold']:.2%}")
    print(f"Max drawdown: {selected['max_drawdown']:.2%}")
    print(f"Turnover/year: {selected['turnover_per_year']:.2f}; changes/year: {selected['changes_per_year']:.2f}")
    print(f"Coverage bias warning: {summary['coverage_bias_warning']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
