#!/usr/bin/env python3
"""Analyze CSI Dividend Low Volatility Index (H30269.CSI) with Tushare data."""

from __future__ import annotations

import argparse
import json
import math
import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import tushare as ts
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parents[1]
RAW_INDEX_DIR = BASE_DIR / "data" / "raw" / "index_daily"
ANALYSIS_DIR = BASE_DIR / "analysis" / "h30269"
DB_PATH = BASE_DIR / "db" / "a_share_factors.duckdb"

TARGET = "H30269.CSI"
BENCHMARKS = {
    "000300.SH": "沪深300",
    "000922.CSI": "中证红利",
}
START_DATE = "20051230"


@dataclass(frozen=True)
class ComponentSpec:
    key: str
    label: str
    weight: float
    value_label: str


COMPONENTS = [
    ComponentSpec("price_pct_3y", "三年价格位置", 0.18, "近3年价格分位"),
    ComponentSpec("dd_3y", "三年回撤位置", 0.12, "距3年高点回撤"),
    ComponentSpec("ma250_dev", "年线偏离", 0.12, "相对250日均线"),
    ComponentSpec("rsi14", "短期拥挤度", 0.10, "RSI14"),
    ComponentSpec("ret_60", "三个月涨幅", 0.10, "60日涨幅"),
    ComponentSpec("ret_250", "一年涨幅", 0.10, "250日涨幅"),
    ComponentSpec("vol_60", "波动风险", 0.10, "60日年化波动"),
    ComponentSpec("rel_250_000300_SH", "相对沪深300热度", 0.09, "一年相对沪深300"),
    ComponentSpec("rel_250_000922_CSI", "相对中证红利热度", 0.09, "一年相对中证红利"),
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build H30269 score and report.")
    parser.add_argument("--start-date", default=START_DATE)
    parser.add_argument("--end-date", default=datetime.now().strftime("%Y%m%d"))
    parser.add_argument("--forward-horizon", type=int, default=120)
    parser.add_argument("--refresh", action="store_true", help="Refetch cached index data.")
    return parser.parse_args()


def init_tushare():
    load_dotenv(BASE_DIR / ".env")
    token = os.getenv("TUSHARE_TOKEN")
    if not token:
        raise SystemExit("Missing TUSHARE_TOKEN")
    ts.set_token(token)
    return ts.pro_api(token)


def fetch_index_daily(pro, ts_code: str, start_date: str, end_date: str, refresh: bool) -> pd.DataFrame:
    RAW_INDEX_DIR.mkdir(parents=True, exist_ok=True)
    cache = RAW_INDEX_DIR / f"{safe_code(ts_code)}_{start_date}_{end_date}.parquet"
    if cache.exists() and not refresh:
        return pd.read_parquet(cache)

    df = pro.index_daily(ts_code=ts_code, start_date=start_date, end_date=end_date)
    if df.empty:
        raise RuntimeError(f"No index_daily data returned for {ts_code}")
    df = df.sort_values("trade_date").reset_index(drop=True)
    df.to_parquet(cache, index=False)
    return df


def fetch_index_weight(pro, end_date: str) -> pd.DataFrame:
    start = str(int(end_date[:4]) - 2) + "0101"
    df = pro.index_weight(index_code=TARGET, start_date=start, end_date=end_date)
    if df.empty:
        raise RuntimeError("No index_weight data returned for H30269.CSI")
    df["trade_date"] = df["trade_date"].astype(str)
    latest = df["trade_date"].max()
    return df[df["trade_date"] == latest].copy()


def fetch_daily_basic_for_date(pro, trade_date: str) -> pd.DataFrame:
    fields = "ts_code,trade_date,close,pe_ttm,pb,dv_ratio,dv_ttm,total_mv,circ_mv"
    return pro.daily_basic(trade_date=trade_date, fields=fields)


def latest_factor_snapshot() -> pd.DataFrame:
    if not DB_PATH.exists():
        return pd.DataFrame()
    with duckdb.connect(str(DB_PATH), read_only=True) as con:
        latest = con.execute("select max(snapshot_trade_date) from factor_daily").fetchone()[0]
        if not latest:
            return pd.DataFrame()
        return con.execute(
            """
            select snapshot_trade_date, ts_code, name, industry, pe_ttm, roe_value,
                   roe_yearly, roe_waa, roe_ann_date, roe_end_date, total_mv
            from factor_daily
            where snapshot_trade_date = ?
            """,
            [latest],
        ).fetchdf()


def safe_code(code: str) -> str:
    return code.replace(".", "_").replace("/", "_")


def rsi(series: pd.Series, window: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / window, adjust=False, min_periods=window).mean()
    avg_loss = loss.ewm(alpha=1 / window, adjust=False, min_periods=window).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def rolling_percentile_last(series: pd.Series, window: int) -> pd.Series:
    def calc(values: np.ndarray) -> float:
        current = values[-1]
        if not np.isfinite(current):
            return np.nan
        valid = values[np.isfinite(values)]
        if len(valid) == 0:
            return np.nan
        return float((valid <= current).sum() / len(valid))

    return series.rolling(window, min_periods=max(60, window // 3)).apply(calc, raw=True)


def historical_percentile(series: pd.Series) -> pd.Series:
    return series.rank(pct=True)


def build_indicator_frame(target: pd.DataFrame, benchmarks: dict[str, pd.DataFrame]) -> pd.DataFrame:
    df = target[["trade_date", "close", "pct_chg"]].copy()
    df["trade_date"] = df["trade_date"].astype(str)
    df["ret_1"] = df["close"].pct_change()

    for code, bench in benchmarks.items():
        key = safe_code(code)
        b = bench[["trade_date", "close"]].rename(columns={"close": f"close_{key}"})
        b["trade_date"] = b["trade_date"].astype(str)
        df = df.merge(b, on="trade_date", how="left")

    df["ma250"] = df["close"].rolling(250, min_periods=120).mean()
    df["ma250_dev"] = df["close"] / df["ma250"] - 1
    df["rolling_high_3y"] = df["close"].rolling(750, min_periods=250).max()
    df["dd_3y"] = df["close"] / df["rolling_high_3y"] - 1
    df["price_pct_3y"] = rolling_percentile_last(df["close"], 750)
    df["rsi14"] = rsi(df["close"])

    for days in [20, 60, 120, 250]:
        df[f"ret_{days}"] = df["close"] / df["close"].shift(days) - 1

    for days in [20, 60, 250]:
        df[f"vol_{days}"] = df["ret_1"].rolling(days, min_periods=max(10, days // 2)).std() * math.sqrt(252)

    for code in benchmarks:
        key = safe_code(code)
        bench_ret = df[f"close_{key}"] / df[f"close_{key}"].shift(250) - 1
        df[f"rel_250_{key}"] = df["ret_250"] - bench_ret

    return df


def add_score(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    scored = df.copy()
    rows = []
    for spec in COMPONENTS:
        pct = historical_percentile(scored[spec.key])
        scored[f"{spec.key}_score"] = pct * 10
        scored[f"{spec.key}_contribution"] = scored[f"{spec.key}_score"] * spec.weight

    contribution_cols = [f"{spec.key}_contribution" for spec in COMPONENTS]
    scored["score"] = scored[contribution_cols].sum(axis=1, min_count=len(COMPONENTS))

    latest = scored.dropna(subset=["score"]).iloc[-1]
    for spec in COMPONENTS:
        rows.append(
            {
                "component": spec.label,
                "metric": spec.value_label,
                "value": latest[spec.key],
                "score": latest[f"{spec.key}_score"],
                "weight": spec.weight,
                "contribution": latest[f"{spec.key}_contribution"],
            }
        )
    return scored, pd.DataFrame(rows)


def add_forward_returns(df: pd.DataFrame, horizons: list[int]) -> pd.DataFrame:
    out = df.copy()
    for horizon in horizons:
        out[f"fwd_{horizon}"] = out["close"].shift(-horizon) / out["close"] - 1
    return out


def zone(score: float) -> str:
    if score <= 3:
        return "<=3 买入区"
    if score >= 7:
        return ">=7 卖出区"
    return "3-7 中性区"


def zone_backtest(scored: pd.DataFrame, horizons: list[int]) -> pd.DataFrame:
    rows = []
    valid = scored.dropna(subset=["score"]).copy()
    valid["zone"] = valid["score"].apply(zone)
    for horizon in horizons:
        col = f"fwd_{horizon}"
        tmp = valid.dropna(subset=[col])
        for z in ["<=3 买入区", "3-7 中性区", ">=7 卖出区"]:
            sub = tmp[tmp["zone"] == z]
            if sub.empty:
                rows.append({"horizon_days": horizon, "zone": z, "samples": 0})
                continue
            rows.append(
                {
                    "horizon_days": horizon,
                    "zone": z,
                    "samples": len(sub),
                    "win_rate": (sub[col] > 0).mean(),
                    "avg_return": sub[col].mean(),
                    "median_return": sub[col].median(),
                    "p10_return": sub[col].quantile(0.10),
                    "p90_return": sub[col].quantile(0.90),
                }
            )
    return pd.DataFrame(rows)


def summary_stats(df: pd.DataFrame) -> dict[str, float | str]:
    valid = df.dropna(subset=["close"]).copy()
    start = valid.iloc[0]
    latest = valid.iloc[-1]
    years = len(valid) / 252
    total_ret = latest["close"] / start["close"] - 1
    cagr = (latest["close"] / start["close"]) ** (1 / years) - 1
    vol = valid["ret_1"].std() * math.sqrt(252)
    max_dd = (valid["close"] / valid["close"].cummax() - 1).min()
    current_dd = latest["close"] / valid["close"].cummax().iloc[-1] - 1
    return {
        "start_date": start["trade_date"],
        "latest_date": latest["trade_date"],
        "latest_close": latest["close"],
        "total_return": total_ret,
        "cagr": cagr,
        "ann_vol": vol,
        "sharpe_0rf": cagr / vol if vol else np.nan,
        "max_drawdown": max_dd,
        "current_drawdown": current_dd,
        "ret_20": latest.get("ret_20"),
        "ret_60": latest.get("ret_60"),
        "ret_120": latest.get("ret_120"),
        "ret_250": latest.get("ret_250"),
    }


def constituent_analysis(pro, latest_date: str) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, float]]:
    weights = fetch_index_weight(pro, latest_date)
    basic = fetch_daily_basic_for_date(pro, latest_date)
    factors = latest_factor_snapshot()
    df = weights.merge(factors, left_on="con_code", right_on="ts_code", how="left")
    df = df.merge(
        basic[["ts_code", "dv_ttm", "pb", "pe_ttm", "total_mv"]].rename(
            columns={"pe_ttm": "pe_ttm_daily", "total_mv": "total_mv_daily"}
        ),
        left_on="con_code",
        right_on="ts_code",
        how="left",
    )
    df["weight_norm"] = df["weight"] / df["weight"].sum()
    df["pe_for_calc"] = df["pe_ttm_daily"].combine_first(df["pe_ttm"])
    df["roe_for_calc"] = df["roe_yearly"].combine_first(df["roe_value"])
    positive_ratio = (df["pe_for_calc"] > 0) & (df["roe_for_calc"] > 0)
    df["earn_ratio"] = np.where(positive_ratio, df["pe_for_calc"] / df["roe_for_calc"], np.nan)

    def wavg(col: str) -> float:
        valid = df[[col, "weight_norm"]].dropna()
        if valid.empty:
            return np.nan
        return float((valid[col] * valid["weight_norm"]).sum() / valid["weight_norm"].sum())

    stats = {
        "constituent_date": weights["trade_date"].max(),
        "constituents": len(weights),
        "top10_weight": float(weights.sort_values("weight", ascending=False).head(10)["weight"].sum()),
        "weighted_pe_ttm": wavg("pe_for_calc"),
        "weighted_pb": wavg("pb"),
        "weighted_roe": wavg("roe_for_calc"),
        "weighted_dividend_yield_ttm": wavg("dv_ttm"),
        "weighted_earn_ratio": wavg("earn_ratio"),
        "earn_ratio_weight_coverage": float(df.loc[positive_ratio, "weight"].sum()),
    }
    industry = (
        df.assign(industry=df["industry"].fillna("未分类"))
        .groupby("industry", as_index=False)["weight"]
        .sum()
        .sort_values("weight", ascending=False)
    )
    return df.sort_values("weight", ascending=False), industry, stats


def pct(value: float | None) -> str:
    if value is None or not np.isfinite(value):
        return "-"
    return f"{value * 100:.2f}%"


def num(value: float | None, digits: int = 2) -> str:
    if value is None or not np.isfinite(value):
        return "-"
    return f"{value:.{digits}f}"


def ymd(value: str) -> str:
    text = str(value)
    if len(text) == 8:
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return text


def markdown_table(df: pd.DataFrame, columns: list[str], formatters: dict[str, callable] | None = None) -> str:
    formatters = formatters or {}
    rows = ["|" + "|".join(columns) + "|", "|" + "|".join(["---"] * len(columns)) + "|"]
    for _, row in df.iterrows():
        values = []
        for col in columns:
            value = row.get(col)
            values.append(str(formatters.get(col, lambda x: x)(value)))
        rows.append("|" + "|".join(values) + "|")
    return "\n".join(rows)


def write_report(
    scored: pd.DataFrame,
    component_latest: pd.DataFrame,
    backtest: pd.DataFrame,
    stats: dict,
    constituents: pd.DataFrame,
    industry: pd.DataFrame,
    constituent_stats: dict,
    end_date: str,
) -> Path:
    ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)
    latest = scored.dropna(subset=["score"]).iloc[-1]
    latest_score = float(latest["score"])
    latest_zone = zone(latest_score)

    top_constituents = constituents.head(15).copy()
    top_constituents["display_name"] = top_constituents["name"].fillna(top_constituents["con_code"])
    top_industry = industry.head(10).copy()

    horizon_120 = backtest[backtest["horizon_days"] == 120].copy()
    current_json = {
        "index_code": TARGET,
        "latest_date": latest["trade_date"],
        "latest_close": float(latest["close"]),
        "score": latest_score,
        "zone": latest_zone,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    (ANALYSIS_DIR / "h30269_latest_score.json").write_text(
        json.dumps(current_json, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    scored.to_csv(ANALYSIS_DIR / "h30269_score_history.csv", index=False, encoding="utf-8-sig")
    component_latest.to_csv(ANALYSIS_DIR / "h30269_score_components_latest.csv", index=False, encoding="utf-8-sig")
    backtest.to_csv(ANALYSIS_DIR / "h30269_score_backtest.csv", index=False, encoding="utf-8-sig")
    constituents.to_csv(ANALYSIS_DIR / "h30269_constituents_latest.csv", index=False, encoding="utf-8-sig")
    industry.to_csv(ANALYSIS_DIR / "h30269_industry_weights_latest.csv", index=False, encoding="utf-8-sig")

    text = f"""# 红利低波 H30269 系统化评分报告

生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

## 当前结论

- 指数代码：{TARGET}
- 最新交易日：{ymd(str(latest['trade_date']))}
- 最新收盘点位：{num(float(latest['close']), 2)}
- 当前评分：**{num(latest_score, 2)} / 10**
- 当前区间：**{latest_zone}**

评分含义：0 分附近代表历史上偏低、偏冷、偏回撤的位置；10 分附近代表历史上偏高、偏热、偏拥挤的位置。分数越低越偏买入观察，分数越高越偏卖出或降低仓位观察。

## 指数长期表现

- 样本起始日：{ymd(str(stats['start_date']))}
- 样本最新日：{ymd(str(stats['latest_date']))}
- 累计收益：{pct(stats['total_return'])}
- 年化收益：{pct(stats['cagr'])}
- 年化波动：{pct(stats['ann_vol'])}
- 0利率夏普：{num(stats['sharpe_0rf'], 2)}
- 历史最大回撤：{pct(stats['max_drawdown'])}
- 当前距历史高点回撤：{pct(stats['current_drawdown'])}
- 近20/60/120/250日涨幅：{pct(stats['ret_20'])} / {pct(stats['ret_60'])} / {pct(stats['ret_120'])} / {pct(stats['ret_250'])}

## 评分函数

总分 = 各指标子分 × 权重后求和。每个子分先转换为历史分位，0 表示历史偏低，10 表示历史偏高。

{markdown_table(
        component_latest.assign(
            value_fmt=component_latest.apply(lambda r: format_component_value(r['metric'], r['value']), axis=1),
            score_fmt=component_latest['score'].map(lambda x: num(x, 2)),
            weight_fmt=component_latest['weight'].map(lambda x: pct(x)),
            contribution_fmt=component_latest['contribution'].map(lambda x: num(x, 2)),
        ),
        ['component', 'metric', 'value_fmt', 'score_fmt', 'weight_fmt', 'contribution_fmt'],
    )}

## 历史胜率回测

下面统计每个评分区间在未来 60/120/250 个交易日后的收益。胜率表示未来收益大于 0 的比例。

{markdown_table(
        backtest.assign(
            win_rate_fmt=backtest['win_rate'].map(pct),
            avg_return_fmt=backtest['avg_return'].map(pct),
            median_return_fmt=backtest['median_return'].map(pct),
            p10_return_fmt=backtest['p10_return'].map(pct),
            p90_return_fmt=backtest['p90_return'].map(pct),
        ),
        ['horizon_days', 'zone', 'samples', 'win_rate_fmt', 'avg_return_fmt', 'median_return_fmt', 'p10_return_fmt', 'p90_return_fmt'],
    )}

## 120日重点阈值

{markdown_table(
        horizon_120.assign(
            win_rate_fmt=horizon_120['win_rate'].map(pct),
            avg_return_fmt=horizon_120['avg_return'].map(pct),
            median_return_fmt=horizon_120['median_return'].map(pct),
        ),
        ['zone', 'samples', 'win_rate_fmt', 'avg_return_fmt', 'median_return_fmt'],
    )}

## 最新成分与估值质量

- 成分权重日期：{ymd(str(constituent_stats['constituent_date']))}
- 成分数量：{int(constituent_stats['constituents'])}
- 前十大权重合计：{num(constituent_stats['top10_weight'], 2)}%
- 成分加权 PE(TTM)：{num(constituent_stats['weighted_pe_ttm'], 2)}
- 成分加权 PB：{num(constituent_stats['weighted_pb'], 2)}
- 成分加权年化 ROE：{num(constituent_stats['weighted_roe'], 2)}%
- 成分加权股息率 TTM：{num(constituent_stats['weighted_dividend_yield_ttm'], 2)}%
- 成分加权市赚率（PE>0 且年化ROE>0）：{num(constituent_stats['weighted_earn_ratio'], 2)}
- 市赚率有效权重覆盖：{num(constituent_stats['earn_ratio_weight_coverage'], 2)}%

### 前十五大成分

{markdown_table(
        top_constituents.assign(
            weight_fmt=top_constituents['weight'].map(lambda x: num(x, 3) + '%'),
            pe_fmt=top_constituents['pe_for_calc'].map(lambda x: num(x, 2)),
            roe_fmt=top_constituents['roe_for_calc'].map(lambda x: num(x, 2) + '%'),
            div_fmt=top_constituents['dv_ttm'].map(lambda x: num(x, 2) + '%'),
        ),
        ['con_code', 'display_name', 'industry', 'weight_fmt', 'pe_fmt', 'roe_fmt', 'div_fmt'],
    )}

### 行业权重前十

{markdown_table(
        top_industry.assign(weight_fmt=top_industry['weight'].map(lambda x: num(x, 2) + '%')),
        ['industry', 'weight_fmt'],
    )}

## 使用提醒

这个评分是历史分位和历史胜率模型，不是保证收益的预测器。红利低波指数本身偏防御，低分更适合做分批买入或提升仓位的信号；高分更适合做止盈、降仓或暂停新增的信号。实际使用时建议结合仓位上限、ETF溢价率、交易成本和个人资金期限。
"""
    report_path = ANALYSIS_DIR / f"h30269_score_report_{end_date}.md"
    report_path.write_text(text, encoding="utf-8")
    (ANALYSIS_DIR / "h30269_score_report_latest.md").write_text(text, encoding="utf-8")
    return report_path


def format_component_value(metric: str, value: float) -> str:
    if metric in {"近3年价格分位", "RSI14"}:
        return num(value, 2)
    if "涨幅" in metric or "波动" in metric or "回撤" in metric or "均线" in metric or "相对" in metric:
        return pct(value)
    return num(value, 2)


def main() -> int:
    args = parse_args()
    pro = init_tushare()
    target = fetch_index_daily(pro, TARGET, args.start_date, args.end_date, args.refresh)
    benchmarks = {}
    for code in BENCHMARKS:
        time.sleep(0.2)
        benchmarks[code] = fetch_index_daily(pro, code, args.start_date, args.end_date, args.refresh)

    indicators = build_indicator_frame(target, benchmarks)
    scored, component_latest = add_score(indicators)
    scored = add_forward_returns(scored, [60, 120, 250])
    backtest = zone_backtest(scored, [60, 120, 250])
    stats = summary_stats(scored)

    latest_date = str(scored.dropna(subset=["score"]).iloc[-1]["trade_date"])
    constituents, industry, constituent_stats = constituent_analysis(pro, latest_date)
    report_path = write_report(
        scored,
        component_latest,
        backtest,
        stats,
        constituents,
        industry,
        constituent_stats,
        latest_date,
    )

    latest = scored.dropna(subset=["score"]).iloc[-1]
    print(f"Report: {report_path}")
    print(f"Latest date: {latest['trade_date']}")
    print(f"Latest close: {latest['close']:.2f}")
    print(f"Score: {latest['score']:.2f}/10 ({zone(float(latest['score']))})")
    print(backtest.to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
