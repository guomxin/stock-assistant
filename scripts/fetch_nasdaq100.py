#!/usr/bin/env python3
"""Fetch Nasdaq-100 (^NDX) daily bars from Yahoo Finance."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

import pandas as pd


BASE_DIR = Path(__file__).resolve().parents[1]
RAW_INDEX_DIR = BASE_DIR / "data" / "raw" / "index_daily"
DEFAULT_START_DATE = "20050101"
DEFAULT_SYMBOL = "^NDX"
OUTPUT_CODE = "NDX.YAHOO"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch Nasdaq-100 index daily data from Yahoo Finance.")
    parser.add_argument("--start-date", default=DEFAULT_START_DATE, help="Start date in YYYYMMDD.")
    parser.add_argument("--end-date", default=datetime.now().strftime("%Y%m%d"), help="End date in YYYYMMDD.")
    parser.add_argument("--symbol", default=DEFAULT_SYMBOL, help="Yahoo Finance symbol. Default: ^NDX.")
    parser.add_argument("--refresh", action="store_true", help="Refetch even if the cache file exists.")
    parser.add_argument("--csv", action="store_true", help="Also write a CSV copy next to the parquet output.")
    return parser.parse_args()


def parse_yyyymmdd(value: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y%m%d").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise SystemExit(f"Invalid date {value!r}; expected YYYYMMDD") from exc


def safe_code(code: str) -> str:
    return code.replace("^", "").replace(".", "_").replace("/", "_")


def cache_path(symbol: str, start_date: str, end_date: str) -> Path:
    return RAW_INDEX_DIR / f"{safe_code(symbol)}_YAHOO_{start_date}_{end_date}.parquet"


def yahoo_chart_url(symbol: str, start_date: str, end_date: str) -> str:
    start_dt = parse_yyyymmdd(start_date)
    # Yahoo period2 is exclusive. Add one day so the requested end date can be included.
    end_dt = parse_yyyymmdd(end_date) + timedelta(days=1)
    period1 = int(start_dt.timestamp())
    period2 = int(end_dt.timestamp())
    return (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        f"{quote(symbol)}?period1={period1}&period2={period2}&interval=1d&events=history"
    )


def fetch_yahoo_daily(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    url = yahoo_chart_url(symbol, start_date, end_date)
    request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))

    error = payload.get("chart", {}).get("error")
    if error:
        raise RuntimeError(f"Yahoo chart error: {error}")

    results = payload.get("chart", {}).get("result") or []
    if not results:
        raise RuntimeError(f"No Yahoo chart data returned for {symbol}")

    result = results[0]
    timestamps = result.get("timestamp") or []
    quote_data = (result.get("indicators", {}).get("quote") or [{}])[0]
    if not timestamps:
        raise RuntimeError(f"No daily timestamps returned for {symbol}")

    df = pd.DataFrame(
        {
            "ts_code": OUTPUT_CODE,
            "trade_date": [
                datetime.fromtimestamp(ts, timezone.utc).strftime("%Y%m%d")
                for ts in timestamps
            ],
            "open": quote_data.get("open", []),
            "close": quote_data.get("close", []),
            "high": quote_data.get("high", []),
            "low": quote_data.get("low", []),
            "vol": quote_data.get("volume", []),
        }
    )
    for column in ["open", "close", "high", "low", "vol"]:
        df[column] = pd.to_numeric(df[column], errors="coerce")

    df = df.dropna(subset=["close"]).sort_values("trade_date").reset_index(drop=True)
    df["pre_close"] = df["close"].shift(1)
    df["change"] = df["close"] - df["pre_close"]
    df["pct_chg"] = df["change"] / df["pre_close"] * 100
    df.loc[df["pre_close"].isna(), ["change", "pct_chg"]] = pd.NA
    return df[
        [
            "ts_code",
            "trade_date",
            "open",
            "close",
            "high",
            "low",
            "pre_close",
            "change",
            "pct_chg",
            "vol",
        ]
    ]


def main() -> int:
    args = parse_args()
    RAW_INDEX_DIR.mkdir(parents=True, exist_ok=True)
    output_path = cache_path(args.symbol, args.start_date, args.end_date)
    if output_path.exists() and not args.refresh:
        df = pd.read_parquet(output_path)
        source = "cache"
    else:
        df = fetch_yahoo_daily(args.symbol, args.start_date, args.end_date)
        if df.empty:
            raise RuntimeError(f"No usable daily rows returned for {args.symbol}")
        df.to_parquet(output_path, index=False)
        source = "yahoo"

    if args.csv:
        df.to_csv(output_path.with_suffix(".csv"), index=False)

    print(f"source={source}")
    print(f"rows={len(df)}")
    print(f"start={df['trade_date'].min()}")
    print(f"end={df['trade_date'].max()}")
    print(f"path={output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
