#!/usr/bin/env python3
"""Fetch current Nasdaq-100 constituents and Yahoo daily bars for each member."""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

import pandas as pd


BASE_DIR = Path(__file__).resolve().parents[1]
CONSTITUENT_DIR = BASE_DIR / "data" / "raw" / "nasdaq100_constituents"
RAW_US_STOCK_DIR = BASE_DIR / "data" / "raw" / "us_stock_daily"

DEFAULT_START_DATE = "20050101"
WIKIPEDIA_URL = "https://en.wikipedia.org/wiki/Nasdaq-100"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch Nasdaq-100 current constituents and Yahoo daily bars.")
    parser.add_argument("--start-date", default=DEFAULT_START_DATE, help="Start date in YYYYMMDD.")
    parser.add_argument("--end-date", default=datetime.now().strftime("%Y%m%d"), help="End date in YYYYMMDD.")
    parser.add_argument("--refresh-constituents", action="store_true", help="Refetch the current constituent list.")
    parser.add_argument("--refresh-prices", action="store_true", help="Refetch prices even if cached.")
    parser.add_argument(
        "--include-historical-changes",
        action="store_true",
        help="Also parse Wikipedia's change table and fetch added/removed historical tickers.",
    )
    parser.add_argument("--allow-errors", action="store_true", help="Return success even if some historical tickers fail.")
    parser.add_argument("--sleep", type=float, default=0.2, help="Seconds to sleep between Yahoo requests.")
    parser.add_argument("--max-symbols", type=int, help="Fetch only the first N symbols, useful for smoke tests.")
    parser.add_argument("--csv", action="store_true", help="Also write CSV copies next to parquet outputs.")
    return parser.parse_args()


def parse_yyyymmdd(value: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y%m%d").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise SystemExit(f"Invalid date {value!r}; expected YYYYMMDD") from exc


def today_yyyymmdd() -> str:
    return datetime.now().strftime("%Y%m%d")


def safe_code(code: str) -> str:
    return code.replace("^", "").replace(".", "_").replace("-", "_").replace("/", "_")


def yahoo_symbol(ticker: str) -> str:
    return str(ticker).strip().replace(".", "-")


def latest_constituent_cache() -> Path | None:
    files = sorted(CONSTITUENT_DIR.glob("nasdaq100_constituents_*.parquet"))
    return files[-1] if files else None


def flatten_column(column) -> str:
    if isinstance(column, tuple):
        return " ".join(str(part) for part in column if str(part) != "nan").strip().lower()
    return str(column).strip().lower()


def read_wikipedia_tables() -> list[pd.DataFrame]:
    request = Request(WIKIPEDIA_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=30) as response:
        html = response.read()
    return pd.read_html(html)


def fetch_current_constituents(refresh: bool) -> pd.DataFrame:
    CONSTITUENT_DIR.mkdir(parents=True, exist_ok=True)
    output_path = CONSTITUENT_DIR / f"nasdaq100_constituents_{today_yyyymmdd()}.parquet"
    if output_path.exists() and not refresh:
        return pd.read_parquet(output_path)

    cached = latest_constituent_cache()
    if cached and not refresh:
        return pd.read_parquet(cached)

    tables = read_wikipedia_tables()
    table = None
    for candidate in tables:
        normalized = {flatten_column(col): col for col in candidate.columns}
        if "ticker" in normalized and "company" in normalized:
            table = candidate.copy()
            table.columns = [flatten_column(col) for col in candidate.columns]
            break
    if table is None:
        raise RuntimeError("Could not find Nasdaq-100 constituent table on Wikipedia.")

    keep = table.copy()
    keep["ticker"] = keep["ticker"].astype(str).str.strip()
    keep["yahoo_symbol"] = keep["ticker"].map(yahoo_symbol)
    keep["company"] = keep["company"].astype(str).str.strip()
    sector_col = None
    industry_col = None
    for column in keep.columns:
        text = str(column).lower()
        if "subsector" in text or "sub-industry" in text:
            industry_col = column
        elif "sector" in text or ("industry" in text and "sub" not in text):
            sector_col = column
    keep["sector"] = keep[sector_col].astype(str).str.strip() if sector_col else pd.NA
    keep["industry"] = keep[industry_col].astype(str).str.strip() if industry_col else pd.NA
    keep["source_url"] = WIKIPEDIA_URL
    keep["fetched_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    keep = keep[["ticker", "yahoo_symbol", "company", "sector", "industry", "source_url", "fetched_at"]]
    keep = keep.drop_duplicates("yahoo_symbol").sort_values("yahoo_symbol").reset_index(drop=True)
    keep.to_parquet(output_path, index=False)
    return keep


def clean_ticker(value) -> str:
    if pd.isna(value):
        return ""
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return ""
    return text


def latest_change_cache() -> Path | None:
    files = sorted(CONSTITUENT_DIR.glob("nasdaq100_changes_*.parquet"))
    return files[-1] if files else None


def fetch_historical_changes(refresh: bool) -> pd.DataFrame:
    CONSTITUENT_DIR.mkdir(parents=True, exist_ok=True)
    output_path = CONSTITUENT_DIR / f"nasdaq100_changes_{today_yyyymmdd()}.parquet"
    if output_path.exists() and not refresh:
        return pd.read_parquet(output_path)

    cached = latest_change_cache()
    if cached and not refresh:
        return pd.read_parquet(cached)

    tables = read_wikipedia_tables()
    table = None
    for candidate in tables:
        normalized = {flatten_column(col): col for col in candidate.columns}
        has_added = any("added" in key and "ticker" in key for key in normalized)
        has_removed = any("removed" in key and "ticker" in key for key in normalized)
        has_date = any(key == "date date" or key == "date" for key in normalized)
        if has_date and has_added and has_removed:
            table = candidate.copy()
            table.columns = [flatten_column(col) for col in candidate.columns]
            break
    if table is None:
        raise RuntimeError("Could not find Nasdaq-100 historical change table on Wikipedia.")

    def find_col(*parts: str) -> str:
        for col in table.columns:
            text = str(col)
            if all(part in text for part in parts):
                return col
        raise KeyError(parts)

    date_col = find_col("date")
    added_ticker_col = find_col("added", "ticker")
    added_security_col = find_col("added", "security")
    removed_ticker_col = find_col("removed", "ticker")
    removed_security_col = find_col("removed", "security")
    reason_col = find_col("reason")

    out = pd.DataFrame(
        {
            "date": table[date_col],
            "added_ticker": table[added_ticker_col].map(clean_ticker),
            "added_security": table[added_security_col].fillna("").astype(str).str.strip(),
            "removed_ticker": table[removed_ticker_col].map(clean_ticker),
            "removed_security": table[removed_security_col].fillna("").astype(str).str.strip(),
            "reason": table[reason_col].fillna("").astype(str).str.strip(),
        }
    )
    parsed = pd.to_datetime(out["date"], errors="coerce")
    out["trade_date"] = parsed.dt.strftime("%Y%m%d")
    out["added_yahoo_symbol"] = out["added_ticker"].map(yahoo_symbol)
    out["removed_yahoo_symbol"] = out["removed_ticker"].map(yahoo_symbol)
    out["source_url"] = WIKIPEDIA_URL
    out["fetched_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    out = out.dropna(subset=["trade_date"]).sort_values("trade_date", ascending=False).reset_index(drop=True)
    out.to_parquet(output_path, index=False)
    return out


def yahoo_chart_url(symbol: str, start_date: str, end_date: str) -> str:
    start_dt = parse_yyyymmdd(start_date)
    end_dt = parse_yyyymmdd(end_date) + timedelta(days=1)
    period1 = int(start_dt.timestamp())
    period2 = int(end_dt.timestamp())
    return (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        f"{quote(symbol)}?period1={period1}&period2={period2}&interval=1d&events=history"
    )


def price_cache_path(symbol: str, start_date: str, end_date: str) -> Path:
    return RAW_US_STOCK_DIR / f"{safe_code(symbol)}_YAHOO_{start_date}_{end_date}.parquet"


def fetch_yahoo_daily(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    request = Request(yahoo_chart_url(symbol, start_date, end_date), headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))

    error = payload.get("chart", {}).get("error")
    if error:
        raise RuntimeError(f"Yahoo chart error for {symbol}: {error}")
    results = payload.get("chart", {}).get("result") or []
    if not results:
        raise RuntimeError(f"No Yahoo chart data returned for {symbol}")

    result = results[0]
    timestamps = result.get("timestamp") or []
    quote_data = (result.get("indicators", {}).get("quote") or [{}])[0]
    adj_data = (result.get("indicators", {}).get("adjclose") or [{}])[0]
    if not timestamps:
        raise RuntimeError(f"No daily timestamps returned for {symbol}")

    df = pd.DataFrame(
        {
            "symbol": symbol,
            "trade_date": [datetime.fromtimestamp(ts, timezone.utc).strftime("%Y%m%d") for ts in timestamps],
            "open": quote_data.get("open", []),
            "close": quote_data.get("close", []),
            "high": quote_data.get("high", []),
            "low": quote_data.get("low", []),
            "vol": quote_data.get("volume", []),
            "adj_close": adj_data.get("adjclose", quote_data.get("close", [])),
        }
    )
    for column in ["open", "close", "high", "low", "vol", "adj_close"]:
        df[column] = pd.to_numeric(df[column], errors="coerce")
    df = df.dropna(subset=["adj_close"]).sort_values("trade_date").reset_index(drop=True)
    df["pre_adj_close"] = df["adj_close"].shift(1)
    df["ret"] = df["adj_close"] / df["pre_adj_close"] - 1
    return df


def main() -> int:
    args = parse_args()
    RAW_US_STOCK_DIR.mkdir(parents=True, exist_ok=True)
    constituents = fetch_current_constituents(args.refresh_constituents)
    symbols = set(constituents["yahoo_symbol"].astype(str))
    if args.include_historical_changes:
        changes = fetch_historical_changes(args.refresh_constituents)
        for column in ["added_yahoo_symbol", "removed_yahoo_symbol"]:
            symbols.update(s for s in changes[column].astype(str) if s and s.lower() != "nan")
        constituents = pd.DataFrame({"yahoo_symbol": sorted(symbols)})
    else:
        constituents = constituents[["yahoo_symbol"]].copy()

    if args.max_symbols:
        constituents = constituents.head(args.max_symbols).copy()

    rows = []
    for i, row in constituents.reset_index(drop=True).iterrows():
        symbol = str(row["yahoo_symbol"])
        path = price_cache_path(symbol, args.start_date, args.end_date)
        source = "cache"
        try:
            if path.exists() and not args.refresh_prices:
                prices = pd.read_parquet(path)
            else:
                prices = fetch_yahoo_daily(symbol, args.start_date, args.end_date)
                if prices.empty:
                    raise RuntimeError("empty price frame")
                prices.to_parquet(path, index=False)
                source = "yahoo"
                if args.csv:
                    prices.to_csv(path.with_suffix(".csv"), index=False)
                time.sleep(max(args.sleep, 0.0))
            rows.append(
                {
                    "symbol": symbol,
                    "rows": len(prices),
                    "start": prices["trade_date"].min(),
                    "end": prices["trade_date"].max(),
                    "source": source,
                    "path": str(path),
                    "error": "",
                }
            )
            print(f"[{i + 1:03d}/{len(constituents):03d}] {symbol}: {source} rows={len(prices)}")
        except Exception as exc:
            rows.append(
                {
                    "symbol": symbol,
                    "rows": 0,
                    "start": "",
                    "end": "",
                    "source": "error",
                    "path": str(path),
                    "error": str(exc),
                }
            )
            print(f"[{i + 1:03d}/{len(constituents):03d}] {symbol}: ERROR {exc}")

    manifest = pd.DataFrame(rows)
    manifest_path = RAW_US_STOCK_DIR / f"nasdaq100_price_manifest_{args.start_date}_{args.end_date}.csv"
    manifest.to_csv(manifest_path, index=False)
    print(f"constituents={len(constituents)}")
    print(f"price_success={(manifest['rows'] > 0).sum()}")
    print(f"price_errors={(manifest['rows'] == 0).sum()}")
    print(f"manifest={manifest_path}")
    return 0 if args.allow_errors or (manifest["rows"] > 0).all() else 2


if __name__ == "__main__":
    raise SystemExit(main())
