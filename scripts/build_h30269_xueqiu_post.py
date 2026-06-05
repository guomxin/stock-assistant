#!/usr/bin/env python3
"""Build a short Xueqiu status for the latest H30269 action signal."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[1]
ANALYSIS_DIR = BASE_DIR / "analysis" / "h30269"
OUT_DIR = ANALYSIS_DIR / "xueqiu_posts"


PREFIX = "$红利低波(CSIH30269)$ $红利低波ETF华泰柏瑞(SH512890)$ $红利低波ETF易方达(SH563020)$"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build H30269 Xueqiu post text.")
    parser.add_argument("session_label", choices=["morning-close", "afternoon-close"])
    parser.add_argument("--output", default="", help="Optional output text path.")
    return parser.parse_args()


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def cn_date(yyyymmdd: str) -> str:
    text = str(yyyymmdd)
    if len(text) == 8 and text.isdigit():
        return f"{int(text[4:6])}月{int(text[6:8])}日"
    return text


def session_text(label: str) -> str:
    if label == "morning-close":
        return "中午收盘"
    return "当日收盘"


def pct(value: float) -> str:
    return f"{value * 100:.2f}%"


def score_zone(score: float) -> str:
    if score <= 3:
        return "高胜率买入观察区"
    if score >= 7:
        return "高分卖出/降仓观察区"
    if score <= 4.0:
        return "偏低估保护区"
    return "中性区"


def action_text(signal: dict) -> str:
    target = float(signal.get("target_position", 0))
    applied = float(signal.get("applied_position", target))
    if abs(target - applied) < 0.005:
        return f"维持 {pct(target)} 仓位"
    if target > applied:
        return f"加仓到 {pct(target)}"
    return f"降仓到 {pct(target)}"


def main() -> int:
    args = parse_args()
    score = read_json(ANALYSIS_DIR / "h30269_latest_score.json")
    summary = read_json(ANALYSIS_DIR / "h30269_recommended_strategy_summary.json")
    signal = summary["current_signal"]

    trade_date = str(signal.get("trade_date") or score.get("latest_date"))
    score_value = float(signal.get("score", score.get("score")))
    text = (
        f"{PREFIX} 截至{cn_date(trade_date)}{session_text(args.session_label)}，"
        f"当前提示：{action_text(signal)}，"
        f"评分状态：{score_value:.2f} / 10，属于{score_zone(score_value)}。"
    )

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    output = Path(args.output) if args.output else OUT_DIR / f"h30269_xueqiu_{trade_date}_{args.session_label}.txt"
    output.write_text(text, encoding="utf-8")

    metadata = {
        "session_label": args.session_label,
        "trade_date": trade_date,
        "score": score_value,
        "target_position": signal.get("target_position"),
        "post_text_path": str(output),
        "post_text": text,
    }
    (output.with_suffix(".json")).write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    print(text)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
