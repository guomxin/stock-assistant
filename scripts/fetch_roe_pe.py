#!/usr/bin/env python3
"""Fetch daily A-share PE(TTM) and latest announced ROE from Tushare."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

import duckdb
import pandas as pd
import tushare as ts
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parents[1]
RAW_DAILY_DIR = BASE_DIR / "data" / "raw" / "daily_basic"
RAW_FINA_DIR = BASE_DIR / "data" / "raw" / "fina_indicator"
FACTOR_DIR = BASE_DIR / "data" / "factors"
DB_PATH = BASE_DIR / "db" / "a_share_factors.duckdb"
LOG_DIR = BASE_DIR / "logs"

DAILY_FIELDS = ",".join(
    [
        "ts_code",
        "trade_date",
        "close",
        "turnover_rate",
        "pe",
        "pe_ttm",
        "pb",
        "ps",
        "ps_ttm",
        "total_share",
        "float_share",
        "total_mv",
        "circ_mv",
    ]
)

STOCK_BASIC_FIELDS = "ts_code,symbol,name,area,industry,market,exchange,list_date"

FINA_FIELDS = ",".join(
    [
        "ts_code",
        "ann_date",
        "end_date",
        "roe",
        "roe_waa",
        "roe_dt",
        "roe_yearly",
        "q_roe",
        "q_dt_roe",
        "update_flag",
    ]
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch A-share PE(TTM) and latest announced ROE from Tushare."
    )
    parser.add_argument(
        "--trade-date",
        help="Trading date in YYYYMMDD. Defaults to latest open trading day up to today.",
    )
    parser.add_argument(
        "--roe-lookback-quarters",
        type=int,
        default=12,
        help="How many quarter-end periods to scan for latest announced ROE.",
    )
    parser.add_argument(
        "--roe-field",
        default="roe_waa",
        choices=["roe", "roe_waa", "roe_dt", "roe_yearly", "q_roe", "q_dt_roe"],
        help="ROE field used as the primary snapshot value.",
    )
    parser.add_argument(
        "--allow-missing-roe",
        action="store_true",
        help="Write PE snapshot even if ROE data cannot be fetched.",
    )
    parser.add_argument(
        "--csv",
        action="store_true",
        help="Also write a CSV copy next to the parquet output.",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=0.35,
        help="Seconds to sleep between Tushare financial-period calls.",
    )
    return parser.parse_args()


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / "fetch_roe_pe.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def init_tushare():
    load_dotenv(BASE_DIR / ".env")
    token = os.getenv("TUSHARE_TOKEN")
    if not token or token == "put_your_tushare_token_here":
        raise SystemExit(
            "Missing TUSHARE_TOKEN. Put it in /mnt/ssd01/stocks/.env or export it."
        )
    ts.set_token(token)
    return ts.pro_api(token)


def latest_trade_date(pro) -> str:
    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=30)).strftime("%Y%m%d")
    cal = pro.trade_cal(
        exchange="",
        start_date=start,
        end_date=end,
        is_open="1",
        fields="cal_date,is_open",
    )
    if cal.empty:
        raise RuntimeError("No open trading day found in the last 30 days.")
    return str(cal["cal_date"].max())


def quarter_periods(asof_yyyymmdd: str, count: int) -> list[str]:
    asof = datetime.strptime(asof_yyyymmdd, "%Y%m%d")
    quarter_ends = [(3, 31), (6, 30), (9, 30), (12, 31)]
    periods: list[str] = []
    year = asof.year
    while len(periods) < count:
        for month, day in reversed(quarter_ends):
            period = datetime(year, month, day)
            if period <= asof:
                periods.append(period.strftime("%Y%m%d"))
                if len(periods) >= count:
                    break
        year -= 1
    return periods


def ensure_dirs() -> None:
    for directory in [RAW_DAILY_DIR, RAW_FINA_DIR, FACTOR_DIR, DB_PATH.parent]:
        directory.mkdir(parents=True, exist_ok=True)


def fetch_daily_basic(pro, trade_date: str) -> pd.DataFrame:
    logging.info("Fetching daily_basic for %s", trade_date)
    df = pro.daily_basic(trade_date=trade_date, fields=DAILY_FIELDS)
    if df.empty:
        raise RuntimeError(f"daily_basic returned no rows for {trade_date}")
    df.to_parquet(RAW_DAILY_DIR / f"daily_basic_{trade_date}.parquet", index=False)
    logging.info("daily_basic rows=%s", len(df))
    return df


def fetch_stock_basic(pro) -> pd.DataFrame:
    logging.info("Fetching stock_basic list_status=L")
    return pro.stock_basic(
        exchange="",
        list_status="L",
        fields=STOCK_BASIC_FIELDS,
    )


def fetch_fina_indicator_vip(
    pro, trade_date: str, periods: Iterable[str], sleep_seconds: float
) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    errors: list[str] = []
    for period in periods:
        logging.info("Fetching fina_indicator_vip period=%s", period)
        try:
            df = pro.query("fina_indicator_vip", period=period, fields=FINA_FIELDS)
        except Exception as exc:  # Tushare surfaces permission/API errors as Exception.
            errors.append(f"{period}: {exc}")
            logging.warning("fina_indicator_vip failed for %s: %s", period, exc)
            break
        if not df.empty:
            df["source_period"] = period
            frames.append(df)
            logging.info("fina_indicator_vip period=%s rows=%s", period, len(df))
        time.sleep(sleep_seconds)

    if not frames:
        details = "; ".join(errors[:2])
        raise RuntimeError(
            "No ROE data fetched from fina_indicator_vip. "
            "Check Tushare points/permission. " + details
        )

    out = pd.concat(frames, ignore_index=True)
    out.to_parquet(RAW_FINA_DIR / f"fina_indicator_asof_{trade_date}.parquet", index=False)
    logging.info("fina_indicator combined rows=%s", len(out))
    return out


def latest_announced_roe(fina: pd.DataFrame, trade_date: str) -> pd.DataFrame:
    if fina.empty:
        return pd.DataFrame(columns=["ts_code"])

    df = fina.copy()
    df["ann_date"] = df["ann_date"].astype(str)
    df["end_date"] = df["end_date"].astype(str)
    df = df[(df["ann_date"].notna()) & (df["ann_date"] != "None")]
    df = df[df["ann_date"] <= trade_date]
    if df.empty:
        return pd.DataFrame(columns=["ts_code"])

    df = df.sort_values(["ts_code", "end_date", "ann_date"])
    df = df.drop_duplicates(subset=["ts_code", "end_date", "ann_date"], keep="last")
    latest = df.groupby("ts_code", as_index=False).tail(1)
    rename = {
        "ann_date": "roe_ann_date",
        "end_date": "roe_end_date",
    }
    latest = latest.rename(columns=rename)
    keep = [
        "ts_code",
        "roe_ann_date",
        "roe_end_date",
        "roe",
        "roe_waa",
        "roe_dt",
        "roe_yearly",
        "q_roe",
        "q_dt_roe",
    ]
    return latest[[col for col in keep if col in latest.columns]]


def build_snapshot(
    daily: pd.DataFrame,
    stock_basic: pd.DataFrame,
    latest_roe: pd.DataFrame,
    trade_date: str,
    roe_field: str,
) -> pd.DataFrame:
    snapshot = daily.merge(stock_basic, on="ts_code", how="left")
    snapshot = snapshot.merge(latest_roe, on="ts_code", how="left")
    if roe_field in snapshot.columns:
        snapshot["roe_value"] = snapshot[roe_field]
        snapshot["roe_value_field"] = roe_field
    else:
        snapshot["roe_value"] = pd.NA
        snapshot["roe_value_field"] = roe_field
    snapshot["snapshot_trade_date"] = trade_date
    snapshot["created_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    front = [
        "snapshot_trade_date",
        "ts_code",
        "symbol",
        "name",
        "market",
        "exchange",
        "industry",
        "close",
        "pe_ttm",
        "roe_value",
        "roe_value_field",
        "roe_ann_date",
        "roe_end_date",
    ]
    ordered = [col for col in front if col in snapshot.columns]
    ordered += [col for col in snapshot.columns if col not in ordered]
    return snapshot[ordered].sort_values("ts_code").reset_index(drop=True)


def write_outputs(snapshot: pd.DataFrame, trade_date: str, write_csv: bool) -> None:
    parquet_path = FACTOR_DIR / f"a_share_roe_pe_{trade_date}.parquet"
    snapshot.to_parquet(parquet_path, index=False)
    logging.info("Wrote %s rows to %s", len(snapshot), parquet_path)

    if write_csv:
        csv_path = FACTOR_DIR / f"a_share_roe_pe_{trade_date}.csv"
        snapshot.to_csv(csv_path, index=False, encoding="utf-8-sig")
        logging.info("Wrote CSV to %s", csv_path)

    with duckdb.connect(str(DB_PATH)) as con:
        con.register("snapshot_df", snapshot)
        con.execute(
            "CREATE TABLE IF NOT EXISTS factor_daily AS "
            "SELECT * FROM snapshot_df WHERE 1=0"
        )
        con.execute("DELETE FROM factor_daily WHERE snapshot_trade_date = ?", [trade_date])
        con.execute("INSERT INTO factor_daily SELECT * FROM snapshot_df")
    logging.info("Upserted DuckDB table factor_daily in %s", DB_PATH)


def main() -> int:
    args = parse_args()
    setup_logging()
    ensure_dirs()
    pro = init_tushare()
    trade_date = args.trade_date or latest_trade_date(pro)
    logging.info("Using trade_date=%s", trade_date)

    daily = fetch_daily_basic(pro, trade_date)
    stock_basic = fetch_stock_basic(pro)

    periods = quarter_periods(trade_date, args.roe_lookback_quarters)
    try:
        fina = fetch_fina_indicator_vip(pro, trade_date, periods, args.sleep)
        latest_roe = latest_announced_roe(fina, trade_date)
    except Exception:
        if not args.allow_missing_roe:
            raise
        logging.exception("ROE fetch failed; continuing because --allow-missing-roe is set.")
        latest_roe = pd.DataFrame(columns=["ts_code"])

    snapshot = build_snapshot(daily, stock_basic, latest_roe, trade_date, args.roe_field)
    write_outputs(snapshot, trade_date, args.csv)
    logging.info("Done. rows=%s roe_non_null=%s", len(snapshot), snapshot["roe_value"].notna().sum())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
