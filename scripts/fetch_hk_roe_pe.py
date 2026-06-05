#!/usr/bin/env python3
"""Fetch HK-share PE(TTM) and ROE snapshots from Tushare."""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import duckdb
import pandas as pd
import tushare as ts
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parents[1]
RAW_HK_BASIC_DIR = BASE_DIR / "data" / "raw" / "hk_basic"
RAW_HK_DAILY_DIR = BASE_DIR / "data" / "raw" / "hk_daily"
RAW_HK_FINA_DIR = BASE_DIR / "data" / "raw" / "hk_fina_indicator"
FACTOR_DIR = BASE_DIR / "data" / "factors"
DB_PATH = BASE_DIR / "db" / "a_share_factors.duckdb"
LOG_DIR = BASE_DIR / "logs"

HK_BASIC_FIELDS = ",".join(
    [
        "ts_code",
        "name",
        "fullname",
        "enname",
        "market",
        "list_status",
        "list_date",
        "delist_date",
        "trade_unit",
        "isin",
        "curr_type",
    ]
)

HK_DAILY_FIELDS = "ts_code,trade_date,open,high,low,close,pre_close,change,pct_chg,vol,amount"

LATEST_KEEP_COLUMNS = [
    "ts_code",
    "end_date",
    "report_type",
    "std_report_date",
    "roe_avg",
    "roe_yearly",
    "pe_ttm",
    "pb_ttm",
    "dividend_rate",
    "eps_ttm",
    "dps_hkd",
    "total_market_cap",
    "hksk_market_cap",
    "issued_common_shares",
    "hk_common_shares",
    "currency",
    "source_fetched_at",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch HK-share PE(TTM) and latest ROE from Tushare."
    )
    parser.add_argument(
        "--trade-date",
        help="HK trading date in YYYYMMDD. Defaults to latest open HK trading day up to today.",
    )
    parser.add_argument(
        "--start-date",
        help="Financial indicator start date in YYYYMMDD. Defaults to four years before trade date.",
    )
    parser.add_argument(
        "--end-date",
        help="Financial indicator end date in YYYYMMDD. Defaults to trade date.",
    )
    parser.add_argument(
        "--refresh-days",
        type=float,
        default=3.0,
        help="Reuse per-stock raw cache if it was fetched within this many days.",
    )
    parser.add_argument(
        "--force-refresh",
        action="store_true",
        help="Ignore raw per-stock cache and call Tushare for every listed HK stock.",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=0.13,
        help="Seconds to sleep after each uncached Tushare hk_fina_indicator call.",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=3,
        help="Retries per stock when Tushare returns a transient or rate-limit error.",
    )
    parser.add_argument(
        "--rate-limit-wait",
        type=float,
        default=65.0,
        help="Seconds to wait before retrying after a Tushare rate-limit error.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Limit number of HK stocks fetched. Useful for smoke tests.",
    )
    parser.add_argument(
        "--csv",
        action="store_true",
        help="Also write a CSV copy next to the parquet output.",
    )
    return parser.parse_args()


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / "fetch_hk_roe_pe.log"
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


def latest_hk_trade_date(pro) -> str:
    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=45)).strftime("%Y%m%d")
    cal = pro.hk_tradecal(
        start_date=start,
        end_date=end,
        is_open="1",
        fields="cal_date,is_open",
    )
    if cal.empty:
        raise RuntimeError("No open HK trading day found in the last 45 days.")
    return str(cal["cal_date"].astype(str).max())


def default_start_date(trade_date: str) -> str:
    date = datetime.strptime(trade_date, "%Y%m%d") - timedelta(days=1460)
    return date.strftime("%Y%m%d")


def ensure_dirs() -> None:
    for directory in [
        RAW_HK_BASIC_DIR,
        RAW_HK_DAILY_DIR,
        RAW_HK_FINA_DIR,
        FACTOR_DIR,
        DB_PATH.parent,
    ]:
        directory.mkdir(parents=True, exist_ok=True)


def fetch_hk_basic(pro, trade_date: str) -> pd.DataFrame:
    logging.info("Fetching hk_basic list_status=L")
    basic = pro.hk_basic(list_status="L", fields=HK_BASIC_FIELDS)
    if basic.empty:
        raise RuntimeError("hk_basic returned no listed HK stocks.")
    basic["symbol"] = basic["ts_code"].astype(str).str.replace(".HK", "", regex=False)
    basic.to_parquet(RAW_HK_BASIC_DIR / f"hk_basic_{trade_date}.parquet", index=False)
    logging.info("hk_basic rows=%s", len(basic))
    return basic


def fetch_hk_daily(
    pro,
    trade_date: str,
    max_retries: int,
    rate_limit_wait: float,
) -> pd.DataFrame:
    cache_path = RAW_HK_DAILY_DIR / f"hk_daily_{trade_date}.parquet"
    if cache_path.exists():
        logging.info("Using cached hk_daily %s", cache_path)
        return pd.read_parquet(cache_path)

    last_error: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            logging.info("Fetching hk_daily trade_date=%s", trade_date)
            daily = pro.hk_daily(trade_date=trade_date, fields=HK_DAILY_FIELDS)
            if daily.empty:
                raise RuntimeError(f"hk_daily returned no rows for {trade_date}")
            daily.to_parquet(cache_path, index=False)
            logging.info("hk_daily rows=%s", len(daily))
            return daily
        except Exception as exc:
            last_error = exc
            if is_rate_limit_error(exc) and attempt < max_retries:
                wait_seconds = rate_limit_wait + attempt * 10
                logging.warning(
                    "Rate limit for hk_daily, retrying in %.0fs (%s/%s): %s",
                    wait_seconds,
                    attempt + 1,
                    max_retries,
                    exc,
                )
                time.sleep(wait_seconds)
                continue
            break
    logging.warning(
        "hk_daily unavailable for %s; will derive price from market cap and shares: %s",
        trade_date,
        last_error,
    )
    return pd.DataFrame(columns=["ts_code"])


def safe_code_filename(ts_code: str) -> str:
    return re.sub(r"[^0-9A-Za-z]+", "_", ts_code).strip("_") + ".parquet"


def cache_is_fresh(path: Path, refresh_days: float) -> bool:
    if not path.exists() or refresh_days <= 0:
        return False
    age_seconds = time.time() - path.stat().st_mtime
    return age_seconds <= refresh_days * 86400


def read_cache(path: Path) -> pd.DataFrame:
    try:
        return pd.read_parquet(path)
    except Exception as exc:
        logging.warning("Ignoring broken cache %s: %s", path, exc)
        return pd.DataFrame()


def is_rate_limit_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "频率超限" in text or "rate" in text or "limit" in text


def fetch_one_indicator(
    pro,
    ts_code: str,
    start_date: str,
    end_date: str,
    refresh_days: float,
    force_refresh: bool,
    sleep_seconds: float,
    max_retries: int,
    rate_limit_wait: float,
) -> tuple[pd.DataFrame, bool]:
    cache_path = RAW_HK_FINA_DIR / safe_code_filename(ts_code)
    if not force_refresh and cache_is_fresh(cache_path, refresh_days):
        cached = read_cache(cache_path)
        if not cached.empty:
            return cached, True

    last_error: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            df = pro.query(
                "hk_fina_indicator",
                ts_code=ts_code,
                start_date=start_date,
                end_date=end_date,
            )
            if df.empty:
                df = pd.DataFrame({"ts_code": [ts_code]})
            df["source_fetched_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            df.to_parquet(cache_path, index=False)
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)
            return df, False
        except Exception as exc:
            last_error = exc
            if is_rate_limit_error(exc) and attempt < max_retries:
                wait_seconds = rate_limit_wait + attempt * 10
                logging.warning(
                    "Rate limit for %s, retrying in %.0fs (%s/%s): %s",
                    ts_code,
                    wait_seconds,
                    attempt + 1,
                    max_retries,
                    exc,
                )
                time.sleep(wait_seconds)
                continue
            break

    if cache_path.exists():
        logging.warning("Fetch failed for %s, using stale cache: %s", ts_code, last_error)
        cached = read_cache(cache_path)
        if not cached.empty:
            return cached, True
    raise last_error or RuntimeError(f"Fetch failed for {ts_code}")


def prepare_concat_frame(df: pd.DataFrame) -> pd.DataFrame:
    return df.dropna(axis=1, how="all")


def fetch_all_indicators(
    pro,
    codes: list[str],
    start_date: str,
    end_date: str,
    refresh_days: float,
    force_refresh: bool,
    sleep_seconds: float,
    max_retries: int,
    rate_limit_wait: float,
) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    fetched = 0
    cached = 0
    errors: list[str] = []

    for index, ts_code in enumerate(codes, start=1):
        try:
            df, from_cache = fetch_one_indicator(
                pro,
                ts_code,
                start_date,
                end_date,
                refresh_days,
                force_refresh,
                sleep_seconds,
                max_retries,
                rate_limit_wait,
            )
            if from_cache:
                cached += 1
            else:
                fetched += 1
            if not df.empty and "end_date" in df.columns:
                frames.append(prepare_concat_frame(df))
        except Exception as exc:
            errors.append(f"{ts_code}: {exc}")
            logging.warning("hk_fina_indicator failed for %s: %s", ts_code, exc)

        if index == 1 or index % 50 == 0 or index == len(codes):
            logging.info(
                "HK indicator progress %s/%s fetched=%s cached=%s errors=%s",
                index,
                len(codes),
                fetched,
                cached,
                len(errors),
            )

    if errors:
        logging.warning("HK indicator errors=%s first=%s", len(errors), errors[:3])
    if not frames:
        raise RuntimeError("No HK financial indicator rows were fetched.")

    combined = pd.concat(frames, ignore_index=True)
    logging.info("hk_fina_indicator combined rows=%s", len(combined))
    return combined


def normalize_date_column(series: pd.Series) -> pd.Series:
    return series.astype(str).str.replace(r"\.0$", "", regex=True)


def latest_indicator(indicators: pd.DataFrame, trade_date: str) -> pd.DataFrame:
    if indicators.empty or "end_date" not in indicators.columns:
        return pd.DataFrame(columns=["ts_code"])

    df = indicators.copy()
    df["end_date"] = normalize_date_column(df["end_date"])
    df = df[df["end_date"].str.fullmatch(r"\d{8}", na=False)]
    df = df[df["end_date"] <= trade_date]
    if df.empty:
        return pd.DataFrame(columns=["ts_code"])

    if "std_report_date" in df.columns:
        df["std_report_date"] = normalize_date_column(df["std_report_date"])
    else:
        df["std_report_date"] = ""

    sort_columns = ["ts_code", "end_date", "std_report_date"]
    if "source_fetched_at" in df.columns:
        sort_columns.append("source_fetched_at")
    df = df.sort_values(sort_columns)
    df = df.drop_duplicates(subset=["ts_code", "end_date"], keep="last")
    latest = df.groupby("ts_code", as_index=False).tail(1)
    keep = [column for column in LATEST_KEEP_COLUMNS if column in latest.columns]
    return latest[keep]


def numeric_series(df: pd.DataFrame, column: str) -> pd.Series:
    if column not in df.columns:
        return pd.Series(pd.NA, index=df.index, dtype="Float64")
    return pd.to_numeric(df[column], errors="coerce")


def build_snapshot(
    basic: pd.DataFrame,
    daily: pd.DataFrame,
    latest: pd.DataFrame,
    trade_date: str,
) -> pd.DataFrame:
    daily_cols = [
        "ts_code",
        "trade_date",
        "open",
        "high",
        "low",
        "close",
        "pre_close",
        "change",
        "pct_chg",
        "vol",
        "amount",
    ]
    daily = daily[[column for column in daily_cols if column in daily.columns]].copy()
    snapshot = basic.merge(daily, on="ts_code", how="left")
    snapshot = snapshot.merge(latest, on="ts_code", how="left")
    if "close" not in snapshot.columns:
        snapshot["close"] = pd.NA
    snapshot["close_source"] = "hk_daily"

    issued_price = numeric_series(snapshot, "total_market_cap") / numeric_series(
        snapshot, "issued_common_shares"
    )
    hksk_price = numeric_series(snapshot, "hksk_market_cap") / numeric_series(
        snapshot, "hk_common_shares"
    )
    close_missing = numeric_series(snapshot, "close").isna()
    use_issued = close_missing & issued_price.notna()
    snapshot.loc[use_issued, "close"] = issued_price.loc[use_issued]
    snapshot.loc[use_issued, "close_source"] = "market_cap_shares"
    close_missing = numeric_series(snapshot, "close").isna()
    use_hksk = close_missing & hksk_price.notna()
    snapshot.loc[use_hksk, "close"] = hksk_price.loc[use_hksk]
    snapshot.loc[use_hksk, "close_source"] = "hksk_market_cap_shares"
    snapshot["roe_value"] = numeric_series(snapshot, "roe_yearly")
    snapshot["roe_value_field"] = "roe_yearly"

    fallback_mask = snapshot["roe_value"].isna()
    if "roe_avg" in snapshot.columns:
        fallback_values = numeric_series(snapshot, "roe_avg")
        use_fallback = fallback_mask & fallback_values.notna()
        snapshot.loc[use_fallback, "roe_value"] = fallback_values.loc[use_fallback]
        snapshot.loc[use_fallback, "roe_value_field"] = "roe_avg"

    snapshot["snapshot_trade_date"] = trade_date
    snapshot["created_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    front = [
        "snapshot_trade_date",
        "ts_code",
        "symbol",
        "name",
        "fullname",
        "enname",
        "market",
        "list_status",
        "list_date",
        "curr_type",
        "trade_date",
        "close",
        "close_source",
        "pe_ttm",
        "roe_value",
        "roe_value_field",
        "roe_yearly",
        "roe_avg",
        "end_date",
        "std_report_date",
        "report_type",
        "pb_ttm",
        "dividend_rate",
        "eps_ttm",
        "dps_hkd",
        "total_market_cap",
        "hksk_market_cap",
        "issued_common_shares",
        "hk_common_shares",
        "currency",
        "source_fetched_at",
        "created_at",
    ]
    ordered = [column for column in front if column in snapshot.columns]
    ordered += [column for column in snapshot.columns if column not in ordered]
    return snapshot[ordered].sort_values("ts_code").reset_index(drop=True)


def quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def duckdb_type(series: pd.Series) -> str:
    if pd.api.types.is_integer_dtype(series):
        return "BIGINT"
    if pd.api.types.is_float_dtype(series):
        return "DOUBLE"
    if pd.api.types.is_bool_dtype(series):
        return "BOOLEAN"
    return "VARCHAR"


def table_columns(con: duckdb.DuckDBPyConnection, table_name: str) -> list[str]:
    rows = con.execute(f"PRAGMA table_info({quote_identifier(table_name)})").fetchall()
    return [row[1] for row in rows]


def ensure_table_schema(
    con: duckdb.DuckDBPyConnection,
    table_name: str,
    snapshot: pd.DataFrame,
) -> list[str]:
    existing_tables = {row[0] for row in con.execute("show tables").fetchall()}
    if table_name not in existing_tables:
        con.execute(
            f"CREATE TABLE {quote_identifier(table_name)} AS "
            "SELECT * FROM snapshot_df WHERE 1=0"
        )
        return table_columns(con, table_name)

    existing_columns = table_columns(con, table_name)
    for column in snapshot.columns:
        if column not in existing_columns:
            con.execute(
                f"ALTER TABLE {quote_identifier(table_name)} "
                f"ADD COLUMN {quote_identifier(column)} {duckdb_type(snapshot[column])}"
            )
    return table_columns(con, table_name)


def write_outputs(snapshot: pd.DataFrame, trade_date: str, write_csv: bool) -> None:
    parquet_path = FACTOR_DIR / f"hk_roe_pe_{trade_date}.parquet"
    snapshot.to_parquet(parquet_path, index=False)
    logging.info("Wrote %s rows to %s", len(snapshot), parquet_path)

    if write_csv:
        csv_path = FACTOR_DIR / f"hk_roe_pe_{trade_date}.csv"
        snapshot.to_csv(csv_path, index=False, encoding="utf-8-sig")
        logging.info("Wrote CSV to %s", csv_path)

    with duckdb.connect(str(DB_PATH)) as con:
        con.register("snapshot_df", snapshot)
        columns = ensure_table_schema(con, "hk_factor_daily", snapshot)
        con.execute("DELETE FROM hk_factor_daily WHERE snapshot_trade_date = ?", [trade_date])
        insert_columns = [column for column in columns if column in snapshot.columns]
        column_sql = ", ".join(quote_identifier(column) for column in insert_columns)
        con.execute(
            f"INSERT INTO hk_factor_daily ({column_sql}) "
            f"SELECT {column_sql} FROM snapshot_df"
        )
    logging.info("Upserted DuckDB table hk_factor_daily in %s", DB_PATH)


def main() -> int:
    args = parse_args()
    setup_logging()
    ensure_dirs()
    pro = init_tushare()
    trade_date = args.trade_date or latest_hk_trade_date(pro)
    start_date = args.start_date or default_start_date(trade_date)
    end_date = args.end_date or trade_date
    logging.info(
        "Using trade_date=%s start_date=%s end_date=%s",
        trade_date,
        start_date,
        end_date,
    )

    basic = fetch_hk_basic(pro, trade_date)
    daily = fetch_hk_daily(pro, trade_date, args.max_retries, args.rate_limit_wait)
    if args.limit:
        basic = basic.head(args.limit).copy()
        logging.info("Applying --limit=%s", args.limit)

    codes = basic["ts_code"].astype(str).tolist()
    indicators = fetch_all_indicators(
        pro,
        codes,
        start_date,
        end_date,
        args.refresh_days,
        args.force_refresh,
        args.sleep,
        args.max_retries,
        args.rate_limit_wait,
    )
    latest = latest_indicator(indicators, trade_date)
    snapshot = build_snapshot(basic, daily, latest, trade_date)
    write_outputs(snapshot, trade_date, args.csv)
    logging.info(
        "Done. rows=%s roe_non_null=%s pe_non_null=%s",
        len(snapshot),
        snapshot["roe_value"].notna().sum(),
        snapshot["pe_ttm"].notna().sum() if "pe_ttm" in snapshot.columns else 0,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
