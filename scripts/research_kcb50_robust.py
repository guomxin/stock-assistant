#!/usr/bin/env python3
"""KCB50 防过拟合策略研究：IS/OOS 切分 + walk-forward 重选模拟 + 参数平台检查.

只读复用 analyze_kcb50_strategy.py 的指标、评分和候选集定义（不新增参数网格，
避免为了"找到更好结果"而数据挖掘）。输出写入 analysis/kcb50/robust_research/，
不修改任何生产文件。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

BASE_DIR = Path(__file__).resolve().parents[1]
RAW_INDEX_DIR = BASE_DIR / "data" / "raw" / "index_daily"
OUT_DIR = BASE_DIR / "analysis" / "kcb50" / "robust_research"

_spec = importlib.util.spec_from_file_location(
    "kcb", BASE_DIR / "scripts" / "analyze_kcb50_strategy.py"
)
kcb = importlib.util.module_from_spec(_spec)
sys.modules["kcb"] = kcb
_spec.loader.exec_module(kcb)

PRODUCTION_NAME = json.loads(
    (BASE_DIR / "config" / "kcb50_production_strategy.json").read_text(encoding="utf-8")
)["strategy_name"]

FAMILY_PARAMS = {
    "trend_low": ["base", "ma", "low_score"],
    "risk_off_high": ["base", "ma", "high_score"],
    "trend_score": ["base", "ma", "low_score", "high_score"],
    "band": ["base", "ma", "low_score", "band"],
    "momentum": ["base", "ret_window", "threshold", "low_score"],
    "pure_momentum": ["base", "ret_window", "threshold"],
    "score_only": ["base", "high_score"],
    "baseline": [],
}

MAX_TURNOVER_PY = kcb.MAX_TURNOVER_PER_YEAR
MAX_CHANGES_PY = kcb.MAX_CHANGES_PER_YEAR
MAX_MDD = kcb.MAX_ACCEPTABLE_DRAWDOWN


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cost", type=float, default=kcb.DEFAULT_COST)
    p.add_argument("--cash-return", type=float, default=kcb.DEFAULT_CASH_RETURN)
    p.add_argument("--is-end", default="2023-12-31", help="样本内截止日（含）")
    p.add_argument("--wf-first-cut", default="2021-12-31", help="walk-forward 第一个选择日")
    return p.parse_args()


def load_latest_cache(ts_code: str) -> pd.DataFrame:
    safe = kcb.safe_code(ts_code)
    files = sorted(RAW_INDEX_DIR.glob(f"{safe}_20191231_*.parquet"))
    if not files:
        raise SystemExit(f"No cached index data for {ts_code}")
    return pd.read_parquet(files[-1])


def build_frame() -> pd.DataFrame:
    target = load_latest_cache(kcb.TARGET)
    benchmarks = {code: load_latest_cache(code) for code in kcb.BENCHMARKS}
    indicators = kcb.build_indicator_frame(target, benchmarks)
    scored, _ = kcb.add_score(indicators)
    df = scored.sort_values("trade_date").dropna(subset=["close", "score"]).reset_index(drop=True)
    df["date"] = pd.to_datetime(df["trade_date"])
    df["daily_ret"] = df["close"].pct_change().fillna(0.0)
    return df


def precompute(df: pd.DataFrame, cost: float, cash_return: float):
    candidates = kcb.generate_candidates(df)
    n = len(df)
    m = len(candidates)
    r = df["daily_ret"].to_numpy(dtype=np.float64)
    cash_daily = (1.0 + cash_return) ** (1 / 252) - 1
    POS = np.empty((m, n), dtype=np.float32)
    TURN = np.empty((m, n), dtype=np.float32)
    RET = np.empty((m, n), dtype=np.float32)
    names, metas = [], []
    for i, (name, sig, meta) in enumerate(candidates):
        s = np.clip(np.nan_to_num(np.asarray(sig, dtype=np.float64), nan=1.0), 0.0, 1.0)
        pos = np.empty(n)
        pos[0] = s[0]
        pos[1:] = s[:-1]
        prev = np.empty(n)
        prev[0] = pos[0]
        prev[1:] = pos[:-1]
        turn = np.abs(pos - prev)
        c = 0.0 if name == "buy_hold" else cost
        POS[i] = pos
        TURN[i] = turn
        RET[i] = pos * r + (1 - pos) * cash_daily - turn * c
        names.append(name)
        metas.append(meta)
    return names, metas, POS, TURN, RET


def window_metrics(RET, TURN, dates, a: int, b: int, chunk: int = 3000) -> dict[str, np.ndarray]:
    m = RET.shape[0]
    years = float((dates[b] - dates[a]) / np.timedelta64(1, "D")) / 365.25
    cagr = np.empty(m)
    mdd = np.empty(m)
    vol = np.empty(m)
    turn_py = np.empty(m)
    chg_py = np.empty(m)
    for c0 in range(0, m, chunk):
        c1 = min(c0 + chunk, m)
        X = RET[c0:c1, a : b + 1].astype(np.float64)
        nav = np.cumprod(1.0 + X, axis=1)
        run = np.maximum.accumulate(nav, axis=1)
        mdd[c0:c1] = (nav / run - 1.0).min(axis=1)
        cagr[c0:c1] = nav[:, -1] ** (1.0 / years) - 1.0
        vol[c0:c1] = X.std(axis=1, ddof=1) * math.sqrt(252)
        T = TURN[c0:c1, a : b + 1].astype(np.float64)
        turn_py[c0:c1] = T.sum(axis=1) / years
        chg_py[c0:c1] = (T > 1e-12).sum(axis=1) / years
    return {"cagr": cagr, "mdd": mdd, "vol": vol, "turn_py": turn_py, "chg_py": chg_py, "years": years}


def build_neighbor_lists(names, metas) -> list[np.ndarray]:
    def key_of(meta):
        params = FAMILY_PARAMS[meta["family"]]
        return (meta["family"], meta["freq"], tuple(round(float(meta[p]), 6) for p in params))

    index_of = {}
    for i, meta in enumerate(metas):
        index_of[key_of(meta)] = i

    grids: dict[tuple[str, str], list[float]] = {}
    for meta in metas:
        for p in FAMILY_PARAMS[meta["family"]]:
            grids.setdefault((meta["family"], p), set()).add(round(float(meta[p]), 6))
    grids = {k: sorted(v) for k, v in grids.items()}

    neighbor_lists = []
    for i, meta in enumerate(metas):
        family, freq = meta["family"], meta["freq"]
        params = FAMILY_PARAMS[family]
        if not params:
            neighbor_lists.append(np.array([i]))
            continue
        options = []
        for p in params:
            grid = grids[(family, p)]
            v = round(float(meta[p]), 6)
            j = grid.index(v)
            options.append(grid[max(0, j - 1) : j + 2])
        found = []
        stack = [()]
        for opts in options:
            stack = [t + (o,) for t in stack for o in opts]
        for combo in stack:
            idx = index_of.get((family, freq, combo))
            if idx is not None:
                found.append(idx)
        neighbor_lists.append(np.array(sorted(set(found))))
    return neighbor_lists


def eligible(m: dict[str, np.ndarray]) -> np.ndarray:
    return (m["turn_py"] <= MAX_TURNOVER_PY) & (m["chg_py"] <= MAX_CHANGES_PY) & (m["mdd"] >= MAX_MDD)


def select_index(rule: str, m: dict[str, np.ndarray], neighbor_lists, nb_cache: dict) -> int:
    ok = eligible(m)
    if rule == "naive_cagr":
        score = m["cagr"]
    elif rule == "sharpe":
        score = np.where(m["vol"] > 0, m["cagr"] / m["vol"], -np.inf)
    elif rule == "plateau":
        key = id(m["cagr"])
        if key not in nb_cache:
            cagr = m["cagr"]
            nb_cache[key] = np.array([cagr[nb].mean() for nb in neighbor_lists])
        score = nb_cache[key]
    else:
        raise ValueError(rule)
    score = np.where(ok, score, -np.inf)
    return int(np.argmax(score))


def stitched_metrics(ret: np.ndarray, dates, a: int, b: int) -> dict[str, float]:
    x = ret[a : b + 1].astype(np.float64)
    nav = np.cumprod(1.0 + x)
    years = float((dates[b] - dates[a]) / np.timedelta64(1, "D")) / 365.25
    dd = nav / np.maximum.accumulate(nav) - 1.0
    return {
        "cagr": float(nav[-1] ** (1.0 / years) - 1.0),
        "total_return": float(nav[-1] - 1.0),
        "max_drawdown": float(dd.min()),
        "years": years,
    }


def walk_forward(rule, cuts, names, POS, TURN, RET, dates, metric_cache, neighbor_lists, nb_cache, cost,
                 fixed_idx: int | None = None):
    n = RET.shape[1]
    picks = []
    stitched = np.empty(0)
    prev_idx = None
    for k, cut in enumerate(cuts):
        if fixed_idx is None:
            if cut not in metric_cache:
                metric_cache[cut] = window_metrics(RET, TURN, dates, 0, cut)
            idx = select_index(rule, metric_cache[cut], neighbor_lists, nb_cache)
        else:
            idx = fixed_idx
        hold_a = cut + 1
        hold_b = cuts[k + 1] if k + 1 < len(cuts) else n - 1
        seg = RET[idx, hold_a : hold_b + 1].astype(np.float64).copy()
        if prev_idx is not None and prev_idx != idx:
            actual_turn = abs(float(POS[idx, hold_a]) - float(POS[prev_idx, cut]))
            own_turn = float(TURN[idx, hold_a])
            seg[0] -= cost * (actual_turn - own_turn)
        stitched = np.concatenate([stitched, seg])
        picks.append({"rule": rule, "selection_date": str(pd.Timestamp(dates[cut]).date()), "picked": names[idx]})
        prev_idx = idx
    a, b = cuts[0] + 1, n - 1
    met = stitched_metrics_from_series(stitched, dates, a, b)
    return picks, met


def stitched_metrics_from_series(x: np.ndarray, dates, a: int, b: int) -> dict[str, float]:
    nav = np.cumprod(1.0 + x)
    years = float((dates[b] - dates[a]) / np.timedelta64(1, "D")) / 365.25
    dd = nav / np.maximum.accumulate(nav) - 1.0
    return {
        "cagr": float(nav[-1] ** (1.0 / years) - 1.0),
        "total_return": float(nav[-1] - 1.0),
        "max_drawdown": float(dd.min()),
        "years": years,
    }


def block_bootstrap_p(diff: np.ndarray, reps: int = 2000, block: int = 20, seed: int = 42) -> float:
    """Moving-block bootstrap: P(mean excess <= 0)."""
    n = len(diff)
    rng = np.random.default_rng(seed)
    nblocks = math.ceil(n / block)
    starts = rng.integers(0, n - block + 1, size=(reps, nblocks))
    idx = (starts[:, :, None] + np.arange(block)[None, None, :]).reshape(reps, -1)[:, :n]
    means = diff[idx].mean(axis=1)
    return float((means <= 0).mean())


def last_trading_index_on_or_before(dates, ts: pd.Timestamp) -> int:
    arr = pd.Series(dates)
    valid = arr[arr <= ts]
    if valid.empty:
        raise SystemExit(f"No trading day on or before {ts}")
    return int(valid.index[-1])


def main() -> int:
    args = parse_args()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    df = build_frame()
    dates = df["date"].to_numpy()
    n = len(df)
    print(f"Frame: {df['trade_date'].iloc[0]} .. {df['trade_date'].iloc[-1]} ({n} rows)")

    print("Generating candidates & precomputing returns ...")
    names, metas, POS, TURN, RET = precompute(df, args.cost, args.cash_return)
    m = len(names)
    name_to_idx = {x: i for i, x in enumerate(names)}
    prod_idx = name_to_idx[PRODUCTION_NAME]
    bh_idx = name_to_idx["buy_hold"]
    print(f"Candidates: {m}")

    print("Building neighbor lists ...")
    neighbor_lists = build_neighbor_lists(names, metas)

    # ---------- A. IS/OOS hard split ----------
    is_end = last_trading_index_on_or_before(dates, pd.Timestamp(args.is_end))
    full = window_metrics(RET, TURN, dates, 0, n - 1)
    m_is = window_metrics(RET, TURN, dates, 0, is_end)
    m_oos = window_metrics(RET, TURN, dates, is_end + 1, n - 1)

    nb_full_cagr = np.array([full["cagr"][nb].mean() for nb in neighbor_lists])
    nb_is_cagr = np.array([m_is["cagr"][nb].mean() for nb in neighbor_lists])
    nb_min_full = np.array([full["cagr"][nb].min() for nb in neighbor_lists])
    nb_size = np.array([len(nb) for nb in neighbor_lists])

    isoos = pd.DataFrame(
        {
            "name": names,
            "family": [x["family"] for x in metas],
            "freq": [x["freq"] for x in metas],
            "full_cagr": full["cagr"],
            "full_mdd": full["mdd"],
            "full_turn_py": full["turn_py"],
            "full_chg_py": full["chg_py"],
            "is_cagr": m_is["cagr"],
            "is_mdd": m_is["mdd"],
            "is_turn_py": m_is["turn_py"],
            "is_chg_py": m_is["chg_py"],
            "oos_cagr": m_oos["cagr"],
            "oos_mdd": m_oos["mdd"],
            "nb_full_cagr_mean": nb_full_cagr,
            "nb_full_cagr_min": nb_min_full,
            "nb_is_cagr_mean": nb_is_cagr,
            "nb_size": nb_size,
        }
    )
    isoos.to_csv(OUT_DIR / "kcb50_isoos_all_candidates.csv", index=False, encoding="utf-8-sig")

    ok_is = (m_is["turn_py"] <= MAX_TURNOVER_PY) & (m_is["chg_py"] <= MAX_CHANGES_PY) & (m_is["mdd"] >= MAX_MDD)
    sub = isoos[ok_is].copy()
    spearman_all = sub["is_cagr"].rank().corr(sub["oos_cagr"].rank())
    top_is = sub.sort_values("is_cagr", ascending=False).head(20)
    top_plateau = sub.sort_values("nb_is_cagr_mean", ascending=False).head(20)
    print(f"IS->OOS Spearman (eligible {len(sub)}): {spearman_all:.3f}")

    # ---------- B. walk-forward ----------
    half_cuts = []
    first = pd.Timestamp(args.wf_first_cut)
    t = first
    last_date = pd.Timestamp(dates[-1])
    while t < last_date:
        half_cuts.append(last_trading_index_on_or_before(dates, t))
        t = t + pd.DateOffset(months=6)
    half_cuts = sorted(set(half_cuts))

    month_cuts = []
    t = first
    while t < last_date:
        month_cuts.append(last_trading_index_on_or_before(dates, t))
        t = (pd.Timestamp(t) + pd.DateOffset(months=1)) + pd.offsets.MonthEnd(0)
    month_cuts = sorted(set(month_cuts))

    metric_cache: dict[int, dict] = {}
    nb_cache: dict = {}
    wf_rows = []
    pick_rows = []
    for rule, cuts, label in [
        ("naive_cagr", half_cuts, "每半年重选-全历史CAGR最高"),
        ("sharpe", half_cuts, "每半年重选-Sharpe最高"),
        ("plateau", half_cuts, "每半年重选-邻域平均CAGR最高"),
        ("naive_cagr", month_cuts, "每月重选-全历史CAGR最高"),
    ]:
        picks, met = walk_forward(
            rule, cuts, names, POS, TURN, RET, dates, metric_cache, neighbor_lists, nb_cache, args.cost
        )
        n_switch = sum(1 for i in range(1, len(picks)) if picks[i]["picked"] != picks[i - 1]["picked"])
        wf_rows.append({"rule": label, "switches": n_switch, **met})
        for p in picks:
            pick_rows.append({**p, "rule": label})

    for fixed, label in [(prod_idx, f"固定生产策略 {PRODUCTION_NAME}"), (bh_idx, "买入持有")]:
        _, met = walk_forward(
            "naive_cagr", half_cuts, names, POS, TURN, RET, dates, metric_cache, neighbor_lists, nb_cache,
            args.cost, fixed_idx=fixed,
        )
        wf_rows.append({"rule": label, "switches": 0, **met})

    wf = pd.DataFrame(wf_rows)
    wf.to_csv(OUT_DIR / "kcb50_walkforward_rules.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(pick_rows).to_csv(OUT_DIR / "kcb50_walkforward_picks.csv", index=False, encoding="utf-8-sig")
    wf_start = str(pd.Timestamp(dates[half_cuts[0] + 1]).date())
    print(f"Walk-forward window: {wf_start} .. {pd.Timestamp(dates[-1]).date()}")
    print(wf.to_string(index=False))

    # ---------- C. finalists ----------
    finalist_names = [PRODUCTION_NAME]
    for nm in top_is["name"].head(3):
        if nm not in finalist_names:
            finalist_names.append(nm)
    for nm in top_plateau["name"].head(3):
        if nm not in finalist_names:
            finalist_names.append(nm)

    fin_rows = []
    for nm in finalist_names:
        i = name_to_idx[nm]
        d_bh = RET[i].astype(np.float64) - RET[bh_idx].astype(np.float64)
        d_prod = RET[i].astype(np.float64) - RET[prod_idx].astype(np.float64)
        oos_slice = slice(is_end + 1, n)
        fin_rows.append(
            {
                "name": nm,
                "family": metas[i]["family"],
                "is_production": nm == PRODUCTION_NAME,
                "full_cagr": full["cagr"][i],
                "full_mdd": full["mdd"][i],
                "is_cagr": m_is["cagr"][i],
                "oos_cagr": m_oos["cagr"][i],
                "oos_mdd": m_oos["mdd"][i],
                "turn_py": full["turn_py"][i],
                "chg_py": full["chg_py"][i],
                "nb_full_cagr_mean": nb_full_cagr[i],
                "nb_full_cagr_min": nb_min_full[i],
                "nb_size": nb_size[i],
                "p_excess_bh_full": block_bootstrap_p(d_bh),
                "p_excess_bh_oos": block_bootstrap_p(d_bh[oos_slice]),
                "p_excess_prod_full": np.nan if nm == PRODUCTION_NAME else block_bootstrap_p(d_prod),
                "p_excess_prod_oos": np.nan if nm == PRODUCTION_NAME else block_bootstrap_p(d_prod[oos_slice]),
            }
        )
    finalists = pd.DataFrame(fin_rows)
    finalists.to_csv(OUT_DIR / "kcb50_finalists.csv", index=False, encoding="utf-8-sig")

    years_list = sorted(set(pd.Timestamp(d).year for d in dates))
    yr_rows = []
    for nm in finalist_names + ["buy_hold"]:
        i = name_to_idx[nm]
        row = {"name": nm}
        for y in years_list:
            mask = pd.Series(dates).dt.year == y
            x = RET[i].astype(np.float64)[mask.to_numpy()]
            row[str(y)] = float(np.prod(1 + x) - 1)
        yr_rows.append(row)
    yearly = pd.DataFrame(yr_rows)
    yearly.to_csv(OUT_DIR / "kcb50_finalists_yearly.csv", index=False, encoding="utf-8-sig")

    # cost sensitivity (x2, x3) for finalists
    cost_rows = []
    for nm in finalist_names:
        i = name_to_idx[nm]
        for mult in [1.0, 2.0, 3.0]:
            extra = TURN[i].astype(np.float64) * args.cost * (mult - 1.0)
            x = RET[i].astype(np.float64) - extra
            met = stitched_metrics_from_series(x, dates, 0, n - 1)
            cost_rows.append({"name": nm, "cost_mult": mult, **met})
    pd.DataFrame(cost_rows).to_csv(OUT_DIR / "kcb50_finalists_cost_sensitivity.csv", index=False, encoding="utf-8-sig")

    summary = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "data_range": [str(df["trade_date"].iloc[0]), str(df["trade_date"].iloc[-1])],
        "candidates": m,
        "is_end": str(pd.Timestamp(dates[is_end]).date()),
        "oos_start": str(pd.Timestamp(dates[is_end + 1]).date()),
        "spearman_is_oos_cagr": float(spearman_all),
        "production": PRODUCTION_NAME,
        "wf_window_start": wf_start,
        "cost": args.cost,
        "cash_return": args.cash_return,
    }
    (OUT_DIR / "kcb50_robust_research_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    top_is.to_csv(OUT_DIR / "kcb50_top20_by_is_cagr.csv", index=False, encoding="utf-8-sig")
    top_plateau.to_csv(OUT_DIR / "kcb50_top20_by_is_plateau.csv", index=False, encoding="utf-8-sig")
    print("Outputs written to", OUT_DIR)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
