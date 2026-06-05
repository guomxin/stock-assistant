#!/usr/bin/env python3
"""Post a prepared status to Xueqiu using browser-cookie authentication."""

from __future__ import annotations

import argparse
import base64
import json
import os
import random
import subprocess
import sys
from datetime import datetime
from http.cookies import SimpleCookie
from pathlib import Path
from urllib import parse, request
from urllib.error import HTTPError, URLError

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parents[1]
LOG_DIR = BASE_DIR / "logs"
HISTORY_PATH = LOG_DIR / "xueqiu_post_history.jsonl"
ALERT_PATH = LOG_DIR / "xueqiu_cookie_alert.json"
DEFAULT_API_URL = "https://xueqiu.com/statuses/update.json"
DEFAULT_CHECK_URL = "https://api.xueqiu.com/statuses/text_check.json"
DEFAULT_TOKEN_URL = "https://xueqiu.com/provider/session/token.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Post H30269 status to Xueqiu.")
    parser.add_argument("--text-file", required=True)
    parser.add_argument("--session-label", required=True)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--validate-only", action="store_true", help="Check auth/text without publishing.")
    return parser.parse_args()


def history_contains(trade_date: str, session_label: str) -> bool:
    if not HISTORY_PATH.exists():
        return False
    for line in HISTORY_PATH.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (
            row.get("trade_date") == trade_date
            and row.get("session_label") == session_label
            and str(row.get("status", "")).startswith("posted")
        ):
            return True
    return False


def append_history(row: dict) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with HISTORY_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def write_alert(reason: str, detail: str, row: dict) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "status": "alert",
        "reason": reason,
        "detail": detail,
        "trade_date": row.get("trade_date"),
        "session_label": row.get("session_label"),
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    ALERT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def clear_alert() -> None:
    ALERT_PATH.unlink(missing_ok=True)


def looks_like_auth_error(message: str) -> bool:
    text = message.lower()
    return any(
        key in text
        for key in ["missing xueqiu_cookie", "unauthorized", "forbidden", "login", "登录", "请先登录", "cookie", "401", "403"]
    )


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def get_xueqiu_cookie() -> str:
    cookie = os.getenv("XUEQIU_COOKIE", "").strip()
    if cookie:
        return cookie
    encoded = os.getenv("XUEQIU_COOKIE_B64", "").strip()
    if not encoded:
        return ""
    return base64.b64decode(encoded).decode("utf-8").strip()


def token_from_cookie(cookie: str, name: str) -> str:
    parsed = SimpleCookie()
    try:
        parsed.load(cookie)
    except Exception:
        return ""
    if name not in parsed:
        return ""
    return parsed[name].value.strip()


def xueqiu_headers(cookie: str, referer: str = "https://xueqiu.com/") -> dict[str, str]:
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Cookie": cookie,
        "Origin": "https://xueqiu.com",
        "Referer": referer,
        "User-Agent": os.getenv(
            "XUEQIU_USER_AGENT",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/125 Safari/537.36",
        ),
        "X-Requested-With": "XMLHttpRequest",
    }
    csrf = os.getenv("XUEQIU_CSRF_TOKEN", "").strip() or token_from_cookie(cookie, "xq_a_token")
    if csrf:
        headers["X-CSRF-Token"] = csrf
    return headers


def call_xueqiu(api_url: str, payload: dict[str, str], cookie: str) -> tuple[int, str]:
    data = parse.urlencode(payload).encode("utf-8")
    req = request.Request(api_url, data=data, headers=xueqiu_headers(cookie), method="POST")
    try:
        with request.urlopen(req, timeout=20) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return resp.status, body
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"xueqiu http {exc.code}: {body[:500]}") from exc
    except URLError as exc:
        raise RuntimeError(f"xueqiu request failed: {exc}") from exc


def get_session_token(api_path: str = "/statuses/update.json") -> str:
    cookie = get_xueqiu_cookie()
    if not cookie:
        raise RuntimeError("missing XUEQIU_COOKIE")
    token_url = os.getenv("XUEQIU_SESSION_TOKEN_URL", DEFAULT_TOKEN_URL).strip() or DEFAULT_TOKEN_URL
    url = f"{token_url}?{parse.urlencode({'api_path': api_path})}"
    req = request.Request(url, headers=xueqiu_headers(cookie), method="GET")
    try:
        with request.urlopen(req, timeout=20) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"xueqiu token http {exc.code}: {body[:500]}") from exc
    except URLError as exc:
        raise RuntimeError(f"xueqiu token request failed: {exc}") from exc
    try:
        token = json.loads(body).get("session_token", "")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"xueqiu token response is not json: {body[:200]}") from exc
    if not token:
        raise RuntimeError(f"xueqiu token response missing session_token: {body[:200]}")
    return token


def refresh_waf_cookie() -> bool:
    refresh_script = BASE_DIR / "scripts" / "xueqiu_waf_refresh.py"
    if not refresh_script.exists():
        return False
    proc = subprocess.run(
        [sys.executable, str(refresh_script), "--write-env"],
        cwd=BASE_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=90,
        check=False,
    )
    if proc.returncode != 0:
        print(proc.stderr[-1000:], file=sys.stderr)
        return False
    load_dotenv(BASE_DIR / ".env", override=True)
    return True


def validate_text(text: str) -> tuple[int, str]:
    cookie = get_xueqiu_cookie()
    if not cookie:
        raise RuntimeError("missing XUEQIU_COOKIE")
    check_url = os.getenv("XUEQIU_TEXT_CHECK_URL", DEFAULT_CHECK_URL).strip() or DEFAULT_CHECK_URL
    return call_xueqiu(
        check_url,
        {
            "text": text,
            "type": "0",
            "x": f"{random.random():.17f}",
        },
        cookie,
    )


def post_status(text: str) -> tuple[int, str]:
    cookie = get_xueqiu_cookie()
    if not cookie:
        raise RuntimeError("missing XUEQIU_COOKIE")
    api_url = os.getenv("XUEQIU_API_URL", "https://xueqiu.com/statuses/update.json").strip() or "https://xueqiu.com/statuses/update.json"
    session_token = get_session_token("/statuses/update.json")
    return call_xueqiu(
        api_url,
        {
            "status": text,
            "allow_reward": "false",
            "ai_disclose": "0",
            "session_token": session_token,
        },
        cookie,
    )


def main() -> int:
    args = parse_args()
    load_dotenv(BASE_DIR / ".env")

    text_path = Path(args.text_file)
    text = text_path.read_text(encoding="utf-8").strip()
    dry_run = args.dry_run or env_bool("XUEQIU_POST_DRY_RUN", False)

    base_row = {
        "trade_date": args.trade_date,
        "session_label": args.session_label,
        "text_file": str(text_path),
        "text": text,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    if history_contains(args.trade_date, args.session_label):
        row = {**base_row, "status": "skipped_duplicate"}
        append_history(row)
        print("skip duplicate Xueqiu post")
        return 0

    if dry_run:
        row = {**base_row, "status": "dry_run"}
        append_history(row)
        print("dry-run Xueqiu post:")
        print(text)
        return 0

    if not get_xueqiu_cookie():
        row = {**base_row, "status": "skipped_missing_cookie"}
        append_history(row)
        write_alert("missing_cookie", "远端未配置 XUEQIU_COOKIE，雪球自动发帖已跳过。", row)
        print("skip Xueqiu post: missing XUEQIU_COOKIE")
        return 0

    if args.validate_only:
        try:
            status_code, body = validate_text(text)
        except Exception as exc:
            row = {**base_row, "status": "validate_failed", "error": str(exc)}
            append_history(row)
            if looks_like_auth_error(str(exc)):
                write_alert("cookie_invalid", "雪球 Cookie 可能已失效，请重新配置。", row)
            else:
                write_alert("post_validate_failed", str(exc)[:300], row)
            print(str(exc), file=sys.stderr)
            return 1
        row = {**base_row, "status": "validated", "http_status": status_code, "response": body[:1000]}
        append_history(row)
        print(f"validated Xueqiu text/auth: http {status_code}")
        return 0

    try:
        status_code, body = post_status(text)
    except Exception as exc:
        if "400019" in str(exc) and refresh_waf_cookie():
            try:
                status_code, body = post_status(text)
            except Exception as retry_exc:
                row = {**base_row, "status": "failed", "error": str(retry_exc), "first_error": str(exc)}
                append_history(row)
                if looks_like_auth_error(str(retry_exc)):
                    write_alert("cookie_invalid", "雪球 Cookie 可能已失效，请重新配置。", row)
                else:
                    write_alert("post_failed", str(retry_exc)[:300], row)
                print(str(retry_exc), file=sys.stderr)
                return 1
        else:
            row = {**base_row, "status": "failed", "error": str(exc)}
            append_history(row)
            if looks_like_auth_error(str(exc)):
                write_alert("cookie_invalid", "雪球 Cookie 可能已失效，请重新配置。", row)
            else:
                write_alert("post_failed", str(exc)[:300], row)
            print(str(exc), file=sys.stderr)
            return 1

    row = {**base_row, "status": "posted", "http_status": status_code, "response": body[:1000]}
    append_history(row)
    clear_alert()
    print(f"posted Xueqiu status: http {status_code}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
