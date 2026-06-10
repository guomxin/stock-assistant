#!/usr/bin/env python3
"""Small LAN query server for A-share and HK-share ROE/PE factors."""

from __future__ import annotations

import argparse
import html
import json
import math
import socket
from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import duckdb
import pandas as pd


BASE_DIR = Path(__file__).resolve().parents[1]
DB_PATH = BASE_DIR / "db" / "a_share_factors.duckdb"
H30269_DIR = BASE_DIR / "analysis" / "h30269"
H30269_DAILY_CLOSE_DIR = H30269_DIR / "daily_close_reports"
KCB50_DIR = BASE_DIR / "analysis" / "kcb50"
KCB50_DAILY_CLOSE_DIR = KCB50_DIR / "daily_close_reports"
XUEQIU_ALERT_PATH = BASE_DIR / "logs" / "xueqiu_cookie_alert.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Serve stock factor query UI.")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host.")
    parser.add_argument("--port", type=int, default=8088, help="Bind port.")
    return parser.parse_args()


@dataclass(frozen=True)
class MarketConfig:
    key: str
    path: str
    title: str
    subtitle: str
    table: str
    latest_column: str
    select_sql: str
    search_columns: tuple[str, ...]


A_SHARE_CONFIG = MarketConfig(
    key="a",
    path="/",
    title="A股 ROE / PE 查询",
    subtitle=(
        "ROE 按百分点展示，例如 3.37 表示 3.37%；默认仅展示 PE 和 ROE 都为正的股票，"
        "搜索时显示完整匹配结果。"
    ),
    table="factor_daily",
    latest_column="snapshot_trade_date",
    select_sql="""
        select
          snapshot_trade_date,
          ts_code,
          symbol,
          name,
          industry,
          market,
          exchange,
          close,
          pe_ttm,
          roe_value,
          roe_value_field,
          roe_ann_date,
          roe_end_date,
          total_mv
        from factor_daily
        where snapshot_trade_date = ?
    """,
    search_columns=("ts_code", "symbol", "name"),
)


HK_SHARE_CONFIG = MarketConfig(
    key="hk",
    path="/hk",
    title="港股 ROE / PE 查询",
    subtitle=(
        "港股 ROE/PE 来自 Tushare hk_fina_indicator，ROE 默认使用 roe_yearly；"
        "默认隐藏负估值、ROE高于50%、PB异常和10亿港元以下市值股票，搜索时显示完整匹配结果。"
    ),
    table="hk_factor_daily",
    latest_column="snapshot_trade_date",
    select_sql="""
        select
          snapshot_trade_date,
          ts_code,
          symbol,
          name,
          fullname,
          enname,
          market,
          list_date,
          close,
          pe_ttm,
          roe_value,
          roe_value_field,
          roe_yearly,
          roe_avg,
          end_date as roe_end_date,
          std_report_date,
          report_type,
          pb_ttm,
          dividend_rate,
          eps_ttm,
          dps_hkd,
          total_market_cap,
          hksk_market_cap,
          currency
        from hk_factor_daily
        where snapshot_trade_date = ?
    """,
    search_columns=("ts_code", "symbol", "name", "fullname", "enname"),
)


MARKETS = {
    A_SHARE_CONFIG.path: A_SHARE_CONFIG,
    "/stocks": A_SHARE_CONFIG,
    HK_SHARE_CONFIG.path: HK_SHARE_CONFIG,
}


class FactorStore:
    def __init__(self, db_path: Path, config: MarketConfig):
        self.db_path = db_path
        self.config = config
        self._mtime = 0.0
        self._latest_date = ""
        self._df = pd.DataFrame()

    def load(self) -> tuple[str, pd.DataFrame]:
        if not self.db_path.exists():
            return "", pd.DataFrame()

        mtime = self.db_path.stat().st_mtime
        if mtime == self._mtime and not self._df.empty:
            return self._latest_date, self._df

        with duckdb.connect(str(self.db_path), read_only=True) as con:
            if not self._table_exists(con):
                self._mtime = mtime
                self._latest_date = ""
                self._df = pd.DataFrame()
                return "", self._df

            latest = con.execute(
                f"select max({self.config.latest_column}) from {self.config.table}"
            ).fetchone()[0]
            if not latest:
                self._mtime = mtime
                self._latest_date = ""
                self._df = pd.DataFrame()
                return "", self._df

            df = con.execute(self.config.select_sql, [latest]).fetchdf()

        df["earn_ratio"] = df.apply(calc_earn_ratio, axis=1)
        df["earn_ratio_eligible"] = df.apply(earn_ratio_eligible, axis=1)
        df["sort_bucket"] = df.apply(
            lambda row: sort_bucket(row.get("earn_ratio"), row.get("earn_ratio_eligible")),
            axis=1,
        )
        df = df.sort_values(
            ["sort_bucket", "earn_ratio", "ts_code"],
            ascending=[True, True, True],
            na_position="last",
        ).reset_index(drop=True)
        df["rank"] = range(1, len(df) + 1)

        self._mtime = mtime
        self._latest_date = str(latest)
        self._df = df
        return self._latest_date, self._df

    def _table_exists(self, con: duckdb.DuckDBPyConnection) -> bool:
        rows = con.execute("show tables").fetchall()
        return self.config.table in {row[0] for row in rows}


def calc_earn_ratio(row: pd.Series) -> float | None:
    pe = to_float(row.get("pe_ttm"))
    roe = to_float(row.get("roe_value"))
    if pe is None or roe is None or roe == 0:
        return None
    # Tushare ROE fields are percentage points, e.g. 10 means 10%.
    return pe / roe


def earn_ratio_eligible(row: pd.Series) -> bool:
    pe = to_float(row.get("pe_ttm"))
    roe = to_float(row.get("roe_value"))
    return pe is not None and roe is not None and pe > 0 and roe > 0


def default_visible(row: pd.Series, config: MarketConfig) -> bool:
    if not earn_ratio_eligible(row):
        return False
    if config.key != "hk":
        return True

    roe = to_float(row.get("roe_value"))
    pb = to_float(row.get("pb_ttm"))
    market_cap = to_float(row.get("total_market_cap"))
    if market_cap is None:
        market_cap = to_float(row.get("hksk_market_cap"))

    return (
        roe is not None
        and roe <= 50
        and pb is not None
        and pb > 0
        and market_cap is not None
        and market_cap >= 1_000_000_000
    )


def sort_bucket(value, eligible: bool) -> int:
    if eligible:
        return 0
    number = to_float(value)
    if number is None:
        return 2
    return 1


def to_float(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def fmt_date(value: str | None) -> str:
    if value is None:
        return "-"
    text = str(value)
    if not text or text == "nan" or text == "NaT":
        return "-"
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return esc(text)


def fmt_number(value, digits: int = 2) -> str:
    number = to_float(value)
    if number is None:
        return "-"
    return f"{number:,.{digits}f}"


def fmt_scaled_number(value, scale: float, digits: int = 2) -> str:
    number = to_float(value)
    if number is None:
        return "-"
    return f"{number / scale:,.{digits}f}"


def fmt_ratio(value) -> str:
    number = to_float(value)
    if number is None:
        return "-"
    return f"{number:.4f}"


def esc(value) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    return html.escape(str(value))


def local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"


class QueryHandler(BaseHTTPRequestHandler):
    stores: dict[str, FactorStore]

    def log_message(self, fmt: str, *args) -> None:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"{timestamp} {self.address_string()} {fmt % args}", flush=True)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self.respond_text("ok\n")
            return
        if parsed.path == "/favicon.ico":
            self.send_response(HTTPStatus.NO_CONTENT)
            self.end_headers()
            return
        if parsed.path == "/h30269":
            self.respond_html(self.render_h30269_report(parsed.query))
            return
        if parsed.path == "/h30269_score":
            self.respond_html(self.render_h30269_report(parsed.query))
            return
        if parsed.path == "/h30269_strategy":
            self.respond_html(self.render_h30269_report(parsed.query))
            return
        if parsed.path == "/h30269_research":
            self.respond_html(
                self.render_markdown_report(
                    "红利低波 H30269 策略研究",
                    BASE_DIR / "analysis" / "h30269" / "h30269_strategy_research_report.md",
                )
            )
            return
        if parsed.path == "/kcb50":
            self.respond_html(self.render_kcb50_report(parsed.query))
            return
        if parsed.path == "/kcb50_research":
            self.respond_html(
                self.render_markdown_report(
                    "科创50 策略研究",
                    KCB50_DIR / "kcb50_strategy_report.md",
                )
            )
            return
        config = MARKETS.get(parsed.path)
        if config is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self.respond_html(self.render_market_page(config, parsed.query))

    def respond_text(self, body: str) -> None:
        raw = body.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def respond_html(self, body: str) -> None:
        raw = body.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def render_market_page(self, config: MarketConfig, query: str) -> str:
        params = parse_qs(query)
        keyword = params.get("q", [""])[0].strip()
        page = clamp_int(params.get("page", ["1"])[0], 1, 100000)
        page_size = clamp_int(params.get("page_size", ["50"])[0], 10, 200)

        latest_date, df = self.stores[config.key].load()
        total_rows = len(df)
        if keyword and not df.empty:
            needle = keyword.casefold()
            mask = pd.Series(False, index=df.index)
            for column in config.search_columns:
                if column in df.columns:
                    mask = mask | df[column].astype(str).str.casefold().str.contains(
                        needle, na=False
                    )
            df = df[mask]
        elif not df.empty:
            df = df[df.apply(lambda row: default_visible(row, config), axis=1)]

        filtered_rows = len(df)
        if not df.empty:
            df = df.copy()
            df["rank"] = range(1, len(df) + 1)
        total_pages = max(1, math.ceil(filtered_rows / page_size))
        page = min(page, total_pages)
        start = (page - 1) * page_size
        end = start + page_size
        rows = df.iloc[start:end]

        return render_page(
            config=config,
            keyword=keyword,
            update_date=fmt_date(latest_date),
            total_rows=total_rows,
            filtered_rows=filtered_rows,
            page=page,
            page_size=page_size,
            total_pages=total_pages,
            generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            access_url=self.public_url(config.path),
            rows=render_rows(rows, config),
            prev_link=page_link(config.path, keyword, page - 1, page_size, page > 1),
            next_link=page_link(config.path, keyword, page + 1, page_size, page < total_pages),
            page_size_options=page_size_options(page_size),
        )

    def public_url(self, path: str) -> str:
        host = self.headers.get("Host", "").strip()
        if host:
            return f"http://{host}{path}"
        return f"http://{local_ip()}:8088{path}"

    def render_markdown_report(self, title: str, report_path: Path) -> str:
        if report_path.exists():
            body = html.escape(report_path.read_text(encoding="utf-8"))
        else:
            body = f"报告尚未生成：{report_path}"
        return REPORT_TEMPLATE.format(
            title=html.escape(title),
            body=body,
            extra=render_xueqiu_alert(),
            generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )

    def render_h30269_report(self, query: str = "") -> str:
        params = parse_qs(query)
        requested_date = params.get("date", [""])[0].strip()
        archive_files = list_h30269_daily_close_reports()

        if requested_date:
            archive_path = H30269_DAILY_CLOSE_DIR / f"h30269_action_report_{requested_date}.md"
            if archive_path.exists():
                body_text = archive_path.read_text(encoding="utf-8").strip()
                title = f"红利低波 H30269 收盘报告 {fmt_date(requested_date)}"
            else:
                body_text = f"没有找到 {fmt_date(requested_date)} 的收盘报告。"
                title = "红利低波 H30269 收盘报告"
        else:
            sections = [
                ("行动提示", H30269_DIR / "h30269_combined_report.md"),
                ("评分明细", H30269_DIR / "h30269_score_report_latest.md"),
                ("策略明细", H30269_DIR / "h30269_recommended_strategy_report.md"),
            ]
            parts: list[str] = []
            for section_title, path in sections:
                if path.exists():
                    text = path.read_text(encoding="utf-8").strip()
                else:
                    text = f"报告尚未生成：{path}"
                parts.append(f"## {section_title}\n\n{text}")
            body_text = "\n\n---\n\n".join(parts)
            title = "红利低波 H30269 行动报告"

        return REPORT_TEMPLATE.format(
            title=html.escape(title),
            body=html.escape(body_text),
            extra=render_xueqiu_alert() + render_h30269_history_controls(archive_files, requested_date),
            generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )

    def render_kcb50_report(self, query: str = "") -> str:
        params = parse_qs(query)
        requested_date = params.get("date", [""])[0].strip()
        archive_files = list_kcb50_daily_close_reports()

        if requested_date:
            archive_path = KCB50_DAILY_CLOSE_DIR / f"kcb50_action_report_{requested_date}.md"
            if archive_path.exists():
                body_text = archive_path.read_text(encoding="utf-8").strip()
                title = f"科创50 收盘报告 {fmt_date(requested_date)}"
            else:
                body_text = f"没有找到 {fmt_date(requested_date)} 的收盘报告。"
                title = "科创50 收盘报告"
        else:
            path = KCB50_DIR / "kcb50_strategy_report.md"
            if path.exists():
                body_text = path.read_text(encoding="utf-8").strip()
            else:
                body_text = f"报告尚未生成：{path}"
            title = "科创50 行动报告"

        return REPORT_TEMPLATE.format(
            title=html.escape(title),
            body=html.escape(body_text),
            extra=render_xueqiu_alert() + render_kcb50_history_controls(archive_files, requested_date),
            generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )


def clamp_int(value: str, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return minimum
    return max(minimum, min(maximum, number))


def page_link(path: str, keyword: str, page: int, page_size: int, enabled: bool) -> str:
    if not enabled:
        return '<span class="pager disabled">不可用</span>'
    href = path + "?" + urlencode({"q": keyword, "page": page, "page_size": page_size})
    return f'<a class="pager" href="{href}">打开</a>'


def page_size_options(current: int) -> str:
    options = []
    for size in [25, 50, 100, 200]:
        selected = " selected" if size == current else ""
        options.append(f'<option value="{size}"{selected}>{size}/页</option>')
    return "\n".join(options)


def render_rows(rows: pd.DataFrame, config: MarketConfig) -> str:
    if rows.empty:
        return table_empty_row(config)
    if config.key == "hk":
        return render_hk_rows(rows)
    return render_a_rows(rows)


def table_empty_row(config: MarketConfig) -> str:
    colspan = 13 if config.key == "hk" else 11
    return f'<tr><td colspan="{colspan}" class="empty">没有匹配的股票</td></tr>'


def ratio_class(value) -> str:
    ratio = to_float(value)
    if ratio is None:
        return "muted"
    if ratio < 0:
        return "negative"
    return ""


def render_a_rows(rows: pd.DataFrame) -> str:
    html_rows: list[str] = []
    for _, row in rows.iterrows():
        html_rows.append(
            "<tr>"
            f"<td class=\"rank\">{int(row['rank'])}</td>"
            f"<td><span class=\"code\">{esc(row.get('ts_code'))}</span></td>"
            f"<td class=\"name\">{esc(row.get('name'))}</td>"
            f"<td>{esc(row.get('industry')) or '-'}</td>"
            f"<td class=\"num\">{fmt_number(row.get('close'), 2)}</td>"
            f"<td class=\"num\">{fmt_number(row.get('pe_ttm'), 2)}</td>"
            f"<td class=\"num\">{fmt_number(row.get('roe_value'), 2)}</td>"
            f"<td class=\"num {ratio_class(row.get('earn_ratio'))}\">{fmt_ratio(row.get('earn_ratio'))}</td>"
            f"<td>{fmt_date(row.get('roe_ann_date'))}</td>"
            f"<td>{fmt_date(row.get('roe_end_date'))}</td>"
            f"<td class=\"num\">{fmt_scaled_number(row.get('total_mv'), 10000, 2)}</td>"
            "</tr>"
        )
    return "\n".join(html_rows)


def render_hk_rows(rows: pd.DataFrame) -> str:
    html_rows: list[str] = []
    for _, row in rows.iterrows():
        market_cap = row.get("total_market_cap")
        if to_float(market_cap) is None:
            market_cap = row.get("hksk_market_cap")
        html_rows.append(
            "<tr>"
            f"<td class=\"rank\">{int(row['rank'])}</td>"
            f"<td><span class=\"code\">{esc(row.get('ts_code'))}</span></td>"
            f"<td class=\"name\">{esc(row.get('name'))}</td>"
            f"<td>{esc(row.get('market')) or '-'}</td>"
            f"<td class=\"num\">{fmt_number(row.get('close'), 3)}</td>"
            f"<td class=\"num\">{fmt_number(row.get('pe_ttm'), 2)}</td>"
            f"<td class=\"num\">{fmt_number(row.get('roe_value'), 2)}</td>"
            f"<td class=\"num {ratio_class(row.get('earn_ratio'))}\">{fmt_ratio(row.get('earn_ratio'))}</td>"
            f"<td class=\"num\">{fmt_number(row.get('pb_ttm'), 2)}</td>"
            f"<td class=\"num\">{fmt_number(row.get('dividend_rate'), 2)}</td>"
            f"<td>{fmt_date(row.get('roe_end_date'))}</td>"
            f"<td>{fmt_date(row.get('std_report_date'))}</td>"
            f"<td class=\"num\">{fmt_scaled_number(market_cap, 100000000, 2)}</td>"
            "</tr>"
        )
    return "\n".join(html_rows)


def list_h30269_daily_close_reports() -> list[str]:
    if not H30269_DAILY_CLOSE_DIR.exists():
        return []
    dates = []
    for path in H30269_DAILY_CLOSE_DIR.glob("h30269_action_report_*.md"):
        date = path.stem.removeprefix("h30269_action_report_")
        if len(date) == 8 and date.isdigit():
            dates.append(date)
    return sorted(set(dates), reverse=True)


def render_h30269_history_controls(archive_dates: list[str], selected_date: str) -> str:
    options = ['<option value="">最新报告</option>']
    for date in archive_dates:
        selected = " selected" if date == selected_date else ""
        options.append(f'<option value="{esc(date)}"{selected}>{fmt_date(date)}</option>')

    latest_links = []
    for date in archive_dates[:20]:
        active = " active" if date == selected_date else ""
        latest_links.append(
            f'<a class="chip{active}" href="/h30269?date={esc(date)}">{fmt_date(date)}</a>'
        )
    links_html = "\n".join(latest_links) if latest_links else '<span class="muted">暂无收盘归档</span>'

    return f"""
    <section class="history-panel">
      <form class="history-form" method="get" action="/h30269">
        <select name="date">
          {"".join(options)}
        </select>
        <button type="submit">查看</button>
        <a class="button secondary" href="/h30269">最新报告</a>
      </form>
      <div class="history-links">{links_html}</div>
    </section>
"""


def list_kcb50_daily_close_reports() -> list[str]:
    if not KCB50_DAILY_CLOSE_DIR.exists():
        return []
    dates = []
    for path in KCB50_DAILY_CLOSE_DIR.glob("kcb50_action_report_*.md"):
        date = path.stem.removeprefix("kcb50_action_report_")
        if len(date) == 8 and date.isdigit():
            dates.append(date)
    return sorted(set(dates), reverse=True)


def render_kcb50_history_controls(archive_dates: list[str], selected_date: str) -> str:
    options = ['<option value="">最新报告</option>']
    for date in archive_dates:
        selected = " selected" if date == selected_date else ""
        options.append(f'<option value="{esc(date)}"{selected}>{fmt_date(date)}</option>')

    latest_links = []
    for date in archive_dates[:20]:
        active = " active" if date == selected_date else ""
        latest_links.append(f'<a class="chip{active}" href="/kcb50?date={esc(date)}">{fmt_date(date)}</a>')
    links_html = "\n".join(latest_links) if latest_links else '<span class="muted">暂无收盘归档</span>'

    return f"""
    <section class="history-panel">
      <form class="history-form" method="get" action="/kcb50">
        <select name="date">
          {"".join(options)}
        </select>
        <button type="submit">查看</button>
        <a class="button secondary" href="/kcb50">最新报告</a>
      </form>
      <div class="history-links">{links_html}</div>
    </section>
"""


def render_xueqiu_alert() -> str:
    if not XUEQIU_ALERT_PATH.exists():
        return ""
    try:
        alert = json.loads(XUEQIU_ALERT_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    reason = esc(alert.get("reason", "xueqiu_alert"))
    detail = esc(alert.get("detail", "雪球自动发帖需要处理。"))
    created_at = esc(alert.get("created_at", ""))
    session_label = esc(alert.get("session_label", ""))
    trade_date = fmt_date(alert.get("trade_date", ""))
    return f"""
    <section class="xueqiu-alert">
      <strong>雪球自动发帖提醒</strong>
      <span>{detail}</span>
      <span class="muted">原因：{reason}；日期：{trade_date}；场次：{session_label}；时间：{created_at}</span>
    </section>
"""


def render_page(
    *,
    config: MarketConfig,
    keyword: str,
    update_date: str,
    total_rows: int,
    filtered_rows: int,
    page: int,
    page_size: int,
    total_pages: int,
    generated_at: str,
    access_url: str,
    rows: str,
    prev_link: str,
    next_link: str,
    page_size_options: str,
) -> str:
    table_head = HK_TABLE_HEAD if config.key == "hk" else A_TABLE_HEAD
    active_a = "active" if config.key == "a" else ""
    active_hk = "active" if config.key == "hk" else ""
    reset_href = config.path
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{esc(config.title)}</title>
  <style>
    :root {{
      color-scheme: light;
      --ink: #17201b;
      --muted: #66736c;
      --line: #d9dfd8;
      --paper: #f7f8f4;
      --panel: #ffffff;
      --accent: #0f766e;
      --accent-soft: #e6f2ef;
      --danger: #b91c1c;
      --shadow: 0 18px 50px rgba(31, 41, 55, .10);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background:
        linear-gradient(90deg, rgba(15,118,110,.08) 1px, transparent 1px),
        linear-gradient(rgba(15,118,110,.06) 1px, transparent 1px),
        var(--paper);
      background-size: 32px 32px;
      color: var(--ink);
      font-family: "Segoe UI", "Microsoft YaHei", "Noto Sans CJK SC", sans-serif;
      letter-spacing: 0;
    }}
    .shell {{ max-width: 1280px; margin: 0 auto; padding: 24px; }}
    .topbar {{
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 18px;
      align-items: end;
      margin-bottom: 18px;
    }}
    h1 {{ margin: 0 0 8px; font-size: 28px; line-height: 1.15; }}
    .subtitle {{ margin: 0; color: var(--muted); font-size: 14px; }}
    .nav {{
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      justify-content: flex-end;
    }}
    .stats {{
      display: grid;
      grid-template-columns: repeat(4, minmax(120px, 1fr));
      gap: 10px;
      margin: 18px 0;
    }}
    .stat {{
      background: rgba(255,255,255,.86);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px 14px;
      box-shadow: var(--shadow);
    }}
    .label {{ color: var(--muted); font-size: 12px; margin-bottom: 6px; }}
    .value {{ font-size: 20px; font-weight: 700; white-space: nowrap; }}
    form {{
      display: grid;
      grid-template-columns: 1fr 120px auto auto;
      gap: 10px;
      margin: 14px 0 16px;
    }}
    input, select, button, .button {{
      height: 40px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      color: var(--ink);
      font: inherit;
      padding: 0 12px;
    }}
    input:focus, select:focus {{
      outline: 2px solid rgba(15,118,110,.22);
      border-color: var(--accent);
    }}
    button, .button {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
      background: var(--accent);
      color: white;
      border-color: var(--accent);
      font-weight: 700;
      text-decoration: none;
      cursor: pointer;
      white-space: nowrap;
    }}
    .button.secondary {{
      color: var(--ink);
      background: #fff;
      border-color: var(--line);
    }}
    .button.active {{
      color: var(--accent);
      background: var(--accent-soft);
      border-color: rgba(15,118,110,.35);
    }}
    .table-wrap {{
      overflow: auto;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: rgba(255,255,255,.94);
      box-shadow: var(--shadow);
    }}
    table {{ width: 100%; border-collapse: collapse; min-width: 1060px; }}
    th, td {{
      border-bottom: 1px solid #e8ece6;
      padding: 10px 12px;
      text-align: left;
      font-size: 13px;
      white-space: nowrap;
    }}
    th {{
      position: sticky;
      top: 0;
      z-index: 1;
      background: #eef4ef;
      color: #334139;
      font-size: 12px;
      font-weight: 800;
    }}
    tr:hover td {{ background: #fbf7ed; }}
    .num, .rank {{ text-align: right; font-variant-numeric: tabular-nums; }}
    .code {{
      font-family: "Cascadia Mono", "Consolas", monospace;
      font-weight: 700;
    }}
    .name {{ font-weight: 700; }}
    .muted {{ color: var(--muted); }}
    .negative {{ color: var(--danger); }}
    .empty {{ text-align: center; color: var(--muted); padding: 42px; }}
    .footer {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      margin: 16px 0 0;
      color: var(--muted);
      font-size: 13px;
    }}
    .pages {{ display: flex; align-items: center; gap: 8px; }}
    .pager {{
      display: inline-flex;
      min-width: 66px;
      height: 34px;
      align-items: center;
      justify-content: center;
      border-radius: 8px;
      border: 1px solid var(--line);
      background: white;
      color: var(--ink);
      text-decoration: none;
    }}
    .disabled {{ opacity: .45; }}
    @media (max-width: 760px) {{
      .shell {{ padding: 16px; }}
      .topbar, form, .stats {{ grid-template-columns: 1fr; }}
      .nav {{ justify-content: flex-start; }}
      h1 {{ font-size: 23px; }}
      .footer {{ flex-direction: column; align-items: flex-start; }}
    }}
  </style>
</head>
<body>
  <main class="shell">
    <section class="topbar">
      <div>
        <h1>{esc(config.title)}</h1>
        <p class="subtitle">{esc(config.subtitle)}</p>
      </div>
      <nav class="nav">
        <a class="button secondary {active_a}" href="/">A股查询</a>
        <a class="button secondary {active_hk}" href="/hk">港股查询</a>
        <a class="button secondary" href="/h30269">红利低波行动报告</a>
        <a class="button secondary" href="/kcb50">科创50行动报告</a>
      </nav>
    </section>

    <section class="stats">
      <div class="stat"><div class="label">数据更新日期</div><div class="value">{update_date}</div></div>
      <div class="stat"><div class="label">全市场股票</div><div class="value">{total_rows}</div></div>
      <div class="stat"><div class="label">当前匹配</div><div class="value">{filtered_rows}</div></div>
      <div class="stat"><div class="label">访问地址</div><div class="value" style="font-size:14px">{esc(access_url)}</div></div>
    </section>

    <form method="get" action="{config.path}">
      <input name="q" value="{esc(keyword)}" placeholder="输入股票代码或名称">
      <select name="page_size">{page_size_options}</select>
      <input type="hidden" name="page" value="1">
      <button type="submit">查询</button>
    </form>

    <section class="table-wrap">
      <table>
        <thead>{table_head}</thead>
        <tbody>{rows}</tbody>
      </table>
    </section>

    <section class="footer">
      <div>第 {page} / {total_pages} 页，每页 {page_size} 条；页面生成于 {generated_at}</div>
      <div class="pages">
        <span>上一页</span>{prev_link}
        <span>下一页</span>{next_link}
      </div>
    </section>
  </main>
</body>
</html>
"""


A_TABLE_HEAD = """
<tr>
  <th class="num">排序</th>
  <th>代码</th>
  <th>名称</th>
  <th>行业</th>
  <th class="num">股价</th>
  <th class="num">PE(TTM)</th>
  <th class="num">ROE(%)</th>
  <th class="num">市赚率</th>
  <th>ROE公告日</th>
  <th>ROE报告期</th>
  <th class="num">总市值(亿元)</th>
</tr>
"""


HK_TABLE_HEAD = """
<tr>
  <th class="num">排序</th>
  <th>代码</th>
  <th>名称</th>
  <th>市场</th>
  <th class="num">股价</th>
  <th class="num">PE(TTM)</th>
  <th class="num">ROE(%)</th>
  <th class="num">市赚率</th>
  <th class="num">PB(TTM)</th>
  <th class="num">股息率(%)</th>
  <th>报告期</th>
  <th>标准报告期</th>
  <th class="num">总市值(亿港元)</th>
</tr>
"""


REPORT_TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    body {{
      margin: 0;
      background: #f7f8f4;
      color: #17201b;
      font-family: "Segoe UI", "Microsoft YaHei", "Noto Sans CJK SC", sans-serif;
      letter-spacing: 0;
    }}
    main {{ max-width: 1120px; margin: 0 auto; padding: 24px; }}
    .bar {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: center;
      margin-bottom: 16px;
      flex-wrap: wrap;
    }}
    a {{ color: #0f766e; font-weight: 700; text-decoration: none; }}
    button, select, .button {{
      height: 38px;
      border: 1px solid #d9dfd8;
      border-radius: 8px;
      background: white;
      color: #17201b;
      font: inherit;
      padding: 0 12px;
    }}
    button, .button {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      background: #0f766e;
      color: white;
      border-color: #0f766e;
      font-weight: 700;
      cursor: pointer;
    }}
    .button.secondary {{
      color: #17201b;
      background: #fff;
      border-color: #d9dfd8;
    }}
    .history-panel {{
      background: white;
      border: 1px solid #d9dfd8;
      border-radius: 8px;
      padding: 14px;
      margin-bottom: 16px;
      box-shadow: 0 18px 50px rgba(31, 41, 55, .08);
    }}
    .history-form {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      align-items: center;
      margin: 0 0 12px;
    }}
    .history-links {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }}
    .chip {{
      display: inline-flex;
      align-items: center;
      min-height: 30px;
      padding: 4px 10px;
      border: 1px solid #d9dfd8;
      border-radius: 8px;
      background: #f7f8f4;
      color: #17201b;
      font-size: 13px;
    }}
    .chip.active {{
      background: #e6f2ef;
      color: #0f766e;
      border-color: rgba(15,118,110,.35);
    }}
    .xueqiu-alert {{
      display: grid;
      gap: 6px;
      background: #fff7ed;
      border: 1px solid #fdba74;
      border-radius: 8px;
      color: #7c2d12;
      padding: 14px;
      margin-bottom: 16px;
      box-shadow: 0 18px 50px rgba(31, 41, 55, .08);
    }}
    pre {{
      white-space: pre-wrap;
      word-break: break-word;
      background: white;
      border: 1px solid #d9dfd8;
      border-radius: 8px;
      padding: 20px;
      line-height: 1.65;
      font-size: 14px;
      box-shadow: 0 18px 50px rgba(31, 41, 55, .10);
    }}
    .muted {{ color: #66736c; font-size: 13px; }}
    @media (max-width: 760px) {{
      main {{ padding: 16px; }}
      .history-form {{ display: grid; grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <main>
    <div class="bar">
      <a href="/">A股查询</a>
      <a href="/hk">港股查询</a>
      <a href="/h30269">红利低波行动报告</a>
      <a href="/kcb50">科创50行动报告</a>
      <span class="muted">页面生成于 {generated_at}</span>
    </div>
    {extra}
    <pre>{body}</pre>
  </main>
</body>
</html>
"""


def main() -> int:
    args = parse_args()
    QueryHandler.stores = {
        A_SHARE_CONFIG.key: FactorStore(DB_PATH, A_SHARE_CONFIG),
        HK_SHARE_CONFIG.key: FactorStore(DB_PATH, HK_SHARE_CONFIG),
    }
    server = ThreadingHTTPServer((args.host, args.port), QueryHandler)
    print(f"Serving on http://{args.host}:{args.port}/", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
