#!/usr/bin/env python3
"""H30269 扩展策略族研究（全收益口径，防过拟合）.

在 research_h30269_robust.py 的 7822 个候选之外，加入教科书级标准策略族
（粗网格，避免围绕近期数据微调）：

- ma_cross / ma_cross_low：双均线交叉（6 个标准快慢对），可叠加低分保护
- vol_target / vol_target_low：波动率目标仓位（目标波动/已实现波动，连续仓位，
  15 个百分点死区控制换手），可叠加低分保护
- dd_control / dd_control_low：250 日高点回撤退出 + 迟滞回补
- cash_momentum / cash_momentum_low：绝对动量、以现金收益为门槛（Antonacci 式）
- score_ladder：评分阶梯仓位（3 个预设档位表）

判定"更好"的预注册标准（缺一不可，先于看结果写定）：
  C1 仅用样本内（2008-2018）数据即可选出（IS 第一名或邻域平台第一名）；
  C2 样本外（2019+）年化高于生产策略，且回撤不差；
  C3 参数邻域全样本年化最差值 > 0（平台而非孤峰）；
  C4 相对生产策略的 OOS 超额 bootstrap P(<=0) < 0.2；
  C5 年均换手 <= 6、年均仓位变化 <= 8（与生产约束一致）。

输出写入 analysis/h30269/robust_research/expanded_*，不修改任何生产文件。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

BASE_DIR = Path(__file__).resolve().parents[1]
OUT_DIR = BASE_DIR / "analysis" / "h30269" / "robust_research"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, BASE_DIR / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


rb = _load("rb", "research_h30269_robust.py")
h30 = sys.modules["h30"]

PRODUCTION_NAME = rb.PRODUCTION_NAME

NEW_FAMILY_PARAMS = {
    "ma_cross": ["fast", "slow", "base"],
    "ma_cross_low": ["fast", "slow", "base", "low_score"],
    "vol_target": ["vol_win", "target"],
    "vol_target_low": ["vol_win", "target", "low_score"],
    "dd_control": ["exit_th", "base"],
    "dd_control_low": ["exit_th", "base", "low_score"],
    "cash_momentum": ["ret_window", "base"],
    "cash_momentum_low": ["ret_window", "base", "low_score"],
    "score_ladder": ["preset_id"],
}
rb.FAMILY_PARAMS.update(NEW_FAMILY_PARAMS)

LADDER_PRESETS = [
    [(3.0, 1.0), (5.0, 0.75), (7.0, 0.5), (99.0, 0.25)],
    [(4.0, 1.0), (6.0, 0.5), (99.0, 0.0)],
    [(3.0, 1.0), (7.0, 0.7), (99.0, 0.3)],
]
VOL_DEADBAND = 0.15


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cost", type=float, default=h30.DEFAULT_COST)
    p.add_argument("--cash-return", type=float, default=h30.DEFAULT_CASH_RETURN)
    p.add_argument("--is-end", default="2018-12-31")
    p.add_argument("--wf-first-cut", default="2013-12-31")
    return p.parse_args()


def build_frame() -> pd.DataFrame:
    """score history 自带 tr_close；尾部无全收益数据的行（盘中估算）截掉，
    中间缺口用价格收益顺延填补."""
    df = h30.load_data()
    if "tr_close" not in df.columns:
        raise SystemExit("score history lacks tr_close; rerun analyze_h30269.py first")
    tr = pd.to_numeric(df["tr_close"], errors="coerce")
    last_valid = tr.last_valid_index()
    df = df.loc[:last_valid].reset_index(drop=True)
    price = df["close"].to_numpy(dtype=float)
    trv = pd.to_numeric(df["tr_close"], errors="coerce").to_numpy(dtype=float)
    rec = np.full(len(df), np.nan)
    for i in range(len(df)):
        if np.isfinite(trv[i]):
            rec[i] = trv[i]
        elif i > 0 and np.isfinite(rec[i - 1]) and price[i - 1] > 0:
            rec[i] = rec[i - 1] * (price[i] / price[i - 1])
    df["tr_nav_close"] = rec
    df["tr_ret"] = pd.Series(rec).pct_change().fillna(0.0).to_numpy()
    df["high_250"] = df["close"].rolling(250, min_periods=120).max()
    df["dd_250"] = df["close"] / df["high_250"] - 1
    return df


def sampled_with_deadband(df: pd.DataFrame, raw: pd.Series, freq: str, band: float) -> np.ndarray:
    sampled = h30.rebalance_signal(df, raw.to_numpy(dtype=float), freq)
    out = np.empty(len(sampled))
    prev = sampled[0]
    for i, v in enumerate(sampled):
        if abs(v - prev) > band:
            prev = v
        out[i] = prev
    return out


def generate_extended_candidates(df: pd.DataFrame, cash_return: float):
    candidates = list(h30.generate_candidates(df))
    close = df["close"]
    score = df["score"]

    def add(name: str, raw, meta: dict, freqs=("daily", "weekly", "monthly")) -> None:
        raw_array = pd.Series(raw, index=df.index).ffill().fillna(1.0).clip(0, 1).to_numpy()
        for freq in freqs:
            candidates.append((f"{freq}_{name}", h30.rebalance_signal(df, raw_array, freq), {**meta, "freq": freq}))

    # 1. 双均线交叉（标准快慢对）
    for fast, slow in [(10, 60), (10, 100), (20, 100), (20, 200), (50, 200), (60, 250)]:
        cross = df[f"ma{fast}"] > df[f"ma{slow}"]
        for base in [0.0, 0.2, 0.4]:
            raw = base + (1 - base) * cross.astype(float)
            add(
                f"ma_cross_f{fast}_s{slow}_base{base:.1f}",
                raw,
                {"family": "ma_cross", "fast": fast, "slow": slow, "base": base},
            )
            raw = base + (1 - base) * ((cross | (score <= 4.0)).astype(float))
            add(
                f"ma_cross_low_f{fast}_s{slow}_base{base:.1f}_low4.0",
                raw,
                {"family": "ma_cross_low", "fast": fast, "slow": slow, "base": base, "low_score": 4.0},
            )

    # 2. 波动率目标仓位（连续仓位 + 死区，周/月再平衡）
    for vol_win in [20, 60]:
        realized = df[f"vol_{vol_win}"]
        for target in [0.10, 0.14, 0.18]:
            raw_pos = (target / realized).clip(0, 1)
            for freq in ("weekly", "monthly"):
                sig = sampled_with_deadband(df, raw_pos.ffill().fillna(1.0), freq, VOL_DEADBAND)
                candidates.append(
                    (
                        f"{freq}_vol_target_w{vol_win}_t{target:.2f}",
                        sig,
                        {"family": "vol_target", "vol_win": vol_win, "target": target, "freq": freq},
                    )
                )
                protected = np.where(score.to_numpy() <= 4.0, 1.0, sig)
                candidates.append(
                    (
                        f"{freq}_vol_target_low_w{vol_win}_t{target:.2f}_low4.0",
                        protected,
                        {
                            "family": "vol_target_low",
                            "vol_win": vol_win,
                            "target": target,
                            "low_score": 4.0,
                            "freq": freq,
                        },
                    )
                )

    # 3. 回撤控制（250 日高点回撤退出 + 迟滞回补）
    for exit_th in [0.10, 0.15, 0.20]:
        off = df["dd_250"] < -exit_th
        on = df["dd_250"] > -exit_th / 2
        state = h30.hysteresis_state(on, off)
        for base in [0.0, 0.3]:
            raw = base + (1 - base) * state
            add(
                f"dd_control_x{exit_th:.2f}_base{base:.1f}",
                raw,
                {"family": "dd_control", "exit_th": exit_th, "base": base},
            )
            raw = base + (1 - base) * np.maximum(state, (score <= 4.0).astype(float))
            add(
                f"dd_control_low_x{exit_th:.2f}_base{base:.1f}_low4.0",
                raw,
                {"family": "dd_control_low", "exit_th": exit_th, "base": base, "low_score": 4.0},
            )

    # 4. 绝对动量，现金收益作门槛（Antonacci 式）
    for n in [60, 120, 250]:
        hurdle = (1 + cash_return) ** (n / 252) - 1
        mom = df[f"ret_{n}"] > hurdle
        for base in [0.0, 0.3]:
            raw = base + (1 - base) * mom.astype(float)
            add(
                f"cash_momentum_ret{n}_base{base:.1f}",
                raw,
                {"family": "cash_momentum", "ret_window": n, "base": base},
            )
            raw = base + (1 - base) * ((mom | (score <= 4.0)).astype(float))
            add(
                f"cash_momentum_low_ret{n}_base{base:.1f}_low4.0",
                raw,
                {"family": "cash_momentum_low", "ret_window": n, "base": base, "low_score": 4.0},
            )

    # 5. 评分阶梯仓位（预设档位表）
    for preset_id, ladder in enumerate(LADDER_PRESETS):
        pos = pd.Series(np.nan, index=df.index)
        remaining = pd.Series(True, index=df.index)
        for threshold, weight in ladder:
            mask = remaining & (score <= threshold)
            pos[mask] = weight
            remaining &= ~mask
        add(
            f"score_ladder_p{preset_id}",
            pos,
            {"family": "score_ladder", "preset_id": preset_id},
        )
    return candidates


def precompute(df: pd.DataFrame, candidates, cost: float, cash_return: float):
    n = len(df)
    m = len(candidates)
    r = df["tr_ret"].to_numpy(dtype=np.float64)
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


def main() -> int:
    args = parse_args()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    df = build_frame()
    dates = df["date"].to_numpy()
    n = len(df)
    print(f"Frame: {df['trade_date'].iloc[0]} .. {df['trade_date'].iloc[-1]} ({n} rows, TR basis)")

    print("Generating extended candidates ...")
    candidates = generate_extended_candidates(df, args.cash_return)
    names, metas, POS, TURN, RET = precompute(df, candidates, args.cost, args.cash_return)
    m = len(names)
    name_to_idx = {x: i for i, x in enumerate(names)}
    prod_idx = name_to_idx[PRODUCTION_NAME]
    bh_idx = name_to_idx["buy_hold"]
    new_mask = np.array([meta["family"] in NEW_FAMILY_PARAMS for meta in metas])
    print(f"Candidates: {m} (new families: {int(new_mask.sum())})")

    neighbor_lists = rb.build_neighbor_lists(names, metas)

    is_end = rb.last_trading_index_on_or_before(dates, pd.Timestamp(args.is_end))
    full = rb.window_metrics(RET, TURN, dates, 0, n - 1)
    m_is = rb.window_metrics(RET, TURN, dates, 0, is_end)
    m_oos = rb.window_metrics(RET, TURN, dates, is_end + 1, n - 1)

    nb_full_cagr = np.array([full["cagr"][nb].mean() for nb in neighbor_lists])
    nb_is_cagr = np.array([m_is["cagr"][nb].mean() for nb in neighbor_lists])
    nb_min_full = np.array([full["cagr"][nb].min() for nb in neighbor_lists])

    isoos = pd.DataFrame(
        {
            "name": names,
            "family": [x["family"] for x in metas],
            "freq": [x["freq"] for x in metas],
            "is_new_family": new_mask,
            "full_cagr": full["cagr"],
            "full_mdd": full["mdd"],
            "full_turn_py": full["turn_py"],
            "full_chg_py": full["chg_py"],
            "is_cagr": m_is["cagr"],
            "is_mdd": m_is["mdd"],
            "oos_cagr": m_oos["cagr"],
            "oos_mdd": m_oos["mdd"],
            "nb_full_cagr_mean": nb_full_cagr,
            "nb_full_cagr_min": nb_min_full,
            "nb_is_cagr_mean": nb_is_cagr,
        }
    )
    isoos.to_csv(OUT_DIR / "expanded_isoos_all_candidates.csv", index=False, encoding="utf-8-sig")

    ok_is = (
        (m_is["turn_py"] <= rb.MAX_TURNOVER_PY)
        & (m_is["chg_py"] <= rb.MAX_CHANGES_PY)
        & (m_is["mdd"] >= rb.MAX_MDD)
    )
    sub = isoos[ok_is].copy()
    top_is = sub.sort_values("is_cagr", ascending=False).head(20)
    top_plateau = sub.sort_values("nb_is_cagr_mean", ascending=False).head(20)
    top_is.to_csv(OUT_DIR / "expanded_top20_by_is_cagr.csv", index=False, encoding="utf-8-sig")
    top_plateau.to_csv(OUT_DIR / "expanded_top20_by_is_plateau.csv", index=False, encoding="utf-8-sig")
    print(f"Eligible (IS constraints): {len(sub)}; new-family among IS top20: {int(top_is['is_new_family'].sum())}, plateau top20: {int(top_plateau['is_new_family'].sum())}")

    # 新策略族单独看：各族最优（按 IS 邻域平均选、报告 OOS）
    fam_rows = []
    for fam in NEW_FAMILY_PARAMS:
        fam_sub = sub[sub["family"] == fam]
        if fam_sub.empty:
            continue
        best = fam_sub.sort_values("nb_is_cagr_mean", ascending=False).iloc[0]
        fam_rows.append(best)
    fam_best = pd.DataFrame(fam_rows)
    fam_best.to_csv(OUT_DIR / "expanded_new_family_best.csv", index=False, encoding="utf-8-sig")

    # walk-forward（扩展全集）
    def build_cuts(months_step: int) -> list[int]:
        cuts = []
        t = pd.Timestamp(args.wf_first_cut)
        last_date = pd.Timestamp(dates[-1])
        while t < last_date:
            cuts.append(rb.last_trading_index_on_or_before(dates, t))
            t = t + pd.DateOffset(months=months_step)
        return sorted(set(cuts))

    year_cuts = build_cuts(12)
    metric_cache: dict[int, dict] = {}
    nb_cache: dict = {}
    wf_rows = []
    pick_rows = []
    for rule, label in [
        ("naive_cagr", "每年重选-全历史CAGR最高(扩展全集)"),
        ("sharpe", "每年重选-Sharpe最高(扩展全集)"),
        ("plateau", "每年重选-邻域平均CAGR最高(扩展全集)"),
    ]:
        picks, met = rb.walk_forward(
            rule, year_cuts, names, POS, TURN, RET, dates, metric_cache, neighbor_lists, nb_cache, args.cost
        )
        n_switch = sum(1 for i in range(1, len(picks)) if picks[i]["picked"] != picks[i - 1]["picked"])
        wf_rows.append({"rule": label, "switches": n_switch, **met})
        pick_rows.extend({**p, "rule": label} for p in picks)
    for fixed, label in [(prod_idx, f"固定生产策略 {PRODUCTION_NAME}"), (bh_idx, "买入持有(全收益)")]:
        _, met = rb.walk_forward(
            "naive_cagr", year_cuts, names, POS, TURN, RET, dates, metric_cache, neighbor_lists, nb_cache,
            args.cost, fixed_idx=fixed,
        )
        wf_rows.append({"rule": label, "switches": 0, **met})
    wf = pd.DataFrame(wf_rows)
    wf.to_csv(OUT_DIR / "expanded_walkforward_rules.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(pick_rows).to_csv(OUT_DIR / "expanded_walkforward_picks.csv", index=False, encoding="utf-8-sig")
    print(wf.to_string(index=False))

    # 决赛圈：生产策略 + IS/平台前 3 + 各新族最优
    finalist_names = [PRODUCTION_NAME]
    for nm in list(top_is["name"].head(3)) + list(top_plateau["name"].head(3)) + list(fam_best["name"]):
        if nm not in finalist_names:
            finalist_names.append(nm)
    fin_rows = []
    oos_slice = slice(is_end + 1, n)
    for nm in finalist_names:
        i = name_to_idx[nm]
        d_bh = RET[i].astype(np.float64) - RET[bh_idx].astype(np.float64)
        d_prod = RET[i].astype(np.float64) - RET[prod_idx].astype(np.float64)
        fin_rows.append(
            {
                "name": nm,
                "family": metas[i]["family"],
                "is_new_family": bool(new_mask[i]),
                "is_production": nm == PRODUCTION_NAME,
                "full_cagr": full["cagr"][i],
                "full_mdd": full["mdd"][i],
                "is_cagr": m_is["cagr"][i],
                "oos_cagr": m_oos["cagr"][i],
                "oos_mdd": m_oos["mdd"][i],
                "turn_py": full["turn_py"][i],
                "chg_py": full["chg_py"][i],
                "nb_full_cagr_min": nb_min_full[i],
                "p_excess_bh_full": rb.block_bootstrap_p(d_bh),
                "p_excess_bh_oos": rb.block_bootstrap_p(d_bh[oos_slice]),
                "p_excess_prod_oos": np.nan if nm == PRODUCTION_NAME else rb.block_bootstrap_p(d_prod[oos_slice]),
            }
        )
    finalists = pd.DataFrame(fin_rows)
    finalists.to_csv(OUT_DIR / "expanded_finalists.csv", index=False, encoding="utf-8-sig")
    print(finalists.to_string(index=False))

    summary = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "basis": "total_return_h20269",
        "data_range": [str(df["trade_date"].iloc[0]), str(df["trade_date"].iloc[-1])],
        "candidates_total": m,
        "candidates_new": int(new_mask.sum()),
        "is_end": str(pd.Timestamp(dates[is_end]).date()),
        "production": PRODUCTION_NAME,
        "criteria": "C1 IS可选出; C2 OOS强于生产且回撤不差; C3 邻域最差>0; C4 OOS超额p<0.2; C5 换手合规",
        "cost": args.cost,
        "cash_return": args.cash_return,
    }
    (OUT_DIR / "expanded_research_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("Outputs written to", OUT_DIR)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
