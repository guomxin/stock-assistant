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
import tempfile
from contextlib import nullcontext
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.cookiejar import Cookie, CookieJar
from http.cookies import SimpleCookie
from pathlib import Path
from urllib import parse, request
from urllib.error import HTTPError, URLError

from dotenv import dotenv_values, load_dotenv, set_key, unset_key


BASE_DIR = Path(__file__).resolve().parents[1]
LOG_DIR = BASE_DIR / "logs"
HISTORY_PATH = LOG_DIR / "xueqiu_post_history.jsonl"
ALERT_PATH = LOG_DIR / "xueqiu_cookie_alert.json"
DEFAULT_API_URL = "https://xueqiu.com/statuses/update.json"
DEFAULT_CHECK_URL = "https://api.xueqiu.com/statuses/text_check.json"
DEFAULT_TOKEN_URL = "https://xueqiu.com/provider/session/token.json"
HTTP_SESSION_PATH = BASE_DIR / "run/xueqiu_http_session.json"


class XueqiuError(RuntimeError):
    def __init__(self, reason: str, message: str, recoverable: bool = False):
        super().__init__(message)
        self.reason = reason
        self.recoverable = recoverable


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


def failure_alert(exc: Exception, row: dict) -> None:
    reason = getattr(exc, "reason", "post_failed")
    details = {
        "cookie_invalid": "雪球登录态已失效，自动刷新未恢复，请重新登录。",
        "verification_required": "雪球触发访问保护，请在浏览器完成人机验证。",
        "waf_blocked": "雪球访问保护未恢复，请检查浏览器验证状态。",
    }
    write_alert(reason, details.get(reason, str(exc)[:300]), row)


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


def save_cookie(cookie: str, user_agent: str | None = None) -> None:
    env_path = BASE_DIR / ".env"
    values = dotenv_values(env_path)
    encoded = base64.b64encode(cookie.encode("utf-8")).decode("ascii")
    if values.get("XUEQIU_COOKIE_B64") != encoded:
        set_key(env_path, "XUEQIU_COOKIE_B64", encoded)
    if "XUEQIU_COOKIE" in values:
        unset_key(env_path, "XUEQIU_COOKIE")
    os.environ.pop("XUEQIU_COOKIE", None)
    os.environ["XUEQIU_COOKIE_B64"] = encoded
    if user_agent:
        if values.get("XUEQIU_USER_AGENT") != user_agent:
            set_key(env_path, "XUEQIU_USER_AGENT", user_agent)
        os.environ["XUEQIU_USER_AGENT"] = user_agent


def session_cookie_header(cookies: list[dict], url: str = "https://www.xueqiu.com/") -> str:
    jar = CookieJar()
    for row in cookies:
        domain = row["domain"]
        expires = row.get("expires", -1)
        jar.set_cookie(Cookie(
            version=0, name=row["name"], value=row["value"],
            port=None, port_specified=False, domain=domain,
            domain_specified=domain.startswith("."), domain_initial_dot=domain.startswith("."),
            path=row.get("path", "/"), path_specified=True,
            secure=row.get("secure", True),
            expires=None if expires < 0 else int(expires), discard=expires < 0,
            comment=None, comment_url=None, rest={}, rfc2109=False,
        ))
    req = request.Request(url)
    jar.add_cookie_header(req)
    return req.get_header("Cookie", "")


def save_http_session(cookies: list[dict], user_agent: str, account_id: str) -> None:
    cookies = [c for c in cookies if c["domain"].lstrip(".") == "xueqiu.com" or c["domain"].endswith(".xueqiu.com")]
    payload = {"cookies": cookies, "user_agent": user_agent, "account_id": account_id}
    HTTP_SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=HTTP_SESSION_PATH.parent, delete=False) as fh:
        temp_path = Path(fh.name)
        json.dump(payload, fh)
    try:
        temp_path.replace(HTTP_SESSION_PATH)
    finally:
        temp_path.unlink(missing_ok=True)


def perform_session_request(req: request.Request, session: dict, playwright=None) -> tuple[int, str, list[dict]]:
    from playwright.sync_api import Error, sync_playwright

    # The cookie jar must select credentials again after cross-host redirects.
    headers = {k: v for k, v in req.header_items() if k.lower() != "cookie"}
    csrf = token_from_cookie(session_cookie_header(session["cookies"]), "xq_a_token")
    if csrf and not os.getenv("XUEQIU_CSRF_TOKEN"):
        headers = {k: v for k, v in headers.items() if k.lower() != "x-csrf-token"}
        headers["X-CSRF-Token"] = csrf
    try:
        with (nullcontext(playwright) if playwright is not None else sync_playwright()) as pw:
            context = pw.request.new_context(
                storage_state={"cookies": session["cookies"], "origins": []},
                user_agent=session["user_agent"],
            )
            try:
                response = context.fetch(
                    req.full_url, method=req.get_method(), headers=headers,
                    data=req.data, timeout=20000, max_retries=0,
                )
                return response.status, response.text(), context.storage_state()["cookies"]
            finally:
                context.dispose()
    except Error as exc:
        raise XueqiuError("request_failed", "雪球 HTTP 会话请求失败，请先确认发布结果") from exc


def read_session_response(req: request.Request, stage: str) -> tuple[int, str, dict]:
    try:
        session = json.loads(HTTP_SESSION_PATH.read_text())
    except (OSError, ValueError) as exc:
        raise XueqiuError("cookie_invalid", "雪球完整会话不可用，请刷新浏览器登录态", stage != "post") from exc
    if not isinstance(session, dict) or not isinstance(session.get("cookies"), list) or not session.get("user_agent"):
        raise XueqiuError("cookie_invalid", "雪球完整会话格式无效，请刷新浏览器登录态", stage != "post")
    expected_user = token_from_cookie(get_xueqiu_cookie(), "u")
    if not expected_user or session.get("account_id") != expected_user:
        raise XueqiuError("account_mismatch", "雪球 HTTP 会话账号与配置不一致，请人工确认")
    status, body, cookies = perform_session_request(req, session)
    result = parse_response(body, stage, status)
    try:
        save_http_session(cookies, session["user_agent"], expected_user)
        save_cookie(session_cookie_header(cookies), session["user_agent"])
    except OSError:
        print("Warning: Xueqiu HTTP session could not be saved", file=sys.stderr)
    return status, body, result


def save_response_cookies(headers) -> None:
    cookies = SimpleCookie()
    cookies.load(get_xueqiu_cookie())
    changed = False
    for header in headers.get_all("Set-Cookie", []):
        received = SimpleCookie()
        received.load(header)
        for name, morsel in received.items():
            domain = morsel["domain"].lstrip(".")
            if domain and domain != "xueqiu.com" and not domain.endswith(".xueqiu.com"):
                continue
            expired = False
            if morsel["max-age"]:
                try:
                    expired = int(morsel["max-age"]) <= 0
                except ValueError:
                    pass
            if not morsel["max-age"] and morsel["expires"]:
                try:
                    expired = parsedate_to_datetime(morsel["expires"]) <= datetime.now(timezone.utc)
                except (ValueError, TypeError):
                    pass
            if expired:
                if name in cookies:
                    del cookies[name]
                    changed = True
            elif name not in cookies or cookies[name].value != morsel.value:
                cookies[name] = morsel.value
                changed = True
    if changed:
        cookie = "; ".join(f"{name}={m.value}" for name, m in cookies.items())
        try:
            save_cookie(cookie)
        except OSError:
            os.environ.pop("XUEQIU_COOKIE", None)
            os.environ["XUEQIU_COOKIE_B64"] = base64.b64encode(cookie.encode()).decode()
            print("Warning: refreshed Xueqiu cookies could not be saved to .env", file=sys.stderr)


def parse_response(body: str, stage: str, http_status: int = 200) -> dict:
    try:
        result = json.loads(body)
    except json.JSONDecodeError as exc:
        if any(marker in body for marker in ("人机验证", "check.xueqiu.com", "访问触发保护")):
            raise XueqiuError("verification_required", "雪球要求完成人机验证", stage != "post") from exc
        raise XueqiuError("invalid_response", f"xueqiu {stage}: expected JSON (http {http_status})") from exc
    if not isinstance(result, dict):
        raise XueqiuError("invalid_response", f"xueqiu {stage}: expected JSON object")
    code = str(result.get("error_code", ""))
    if code not in ("", "0") or result.get("success") is False or result.get("error"):
        reason = "cookie_invalid" if code == "400016" or http_status == 401 else "api_error"
        if code == "400019":
            reason = "waf_blocked"
        detail = str(result.get("error_description") or result.get("error") or "request rejected")[:200]
        # Only retry explicit rejections, never an ambiguous publication result.
        recoverable = code == "400019" or (reason == "cookie_invalid" and stage != "post")
        raise XueqiuError(reason, f"xueqiu {stage}: {code} {detail}", recoverable)
    if http_status >= 400:
        raise XueqiuError("request_failed", f"xueqiu {stage}: http {http_status}")
    return result


def read_response(req: request.Request, stage: str) -> tuple[int, str, dict]:
    if HTTP_SESSION_PATH.exists():
        return read_session_response(req, stage)
    try:
        with request.urlopen(req, timeout=20) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            save_response_cookies(resp.headers)
            return resp.status, body, parse_response(body, stage, resp.status)
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        save_response_cookies(exc.headers)
        parse_response(body, stage, exc.code)
        raise XueqiuError("request_failed", f"xueqiu {stage}: http {exc.code}") from exc
    except URLError as exc:
        raise XueqiuError("request_failed", f"xueqiu {stage}: network request failed") from exc


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


def call_xueqiu(api_url: str, payload: dict[str, str], cookie: str, stage: str = "post") -> tuple[int, str]:
    data = parse.urlencode(payload).encode("utf-8")
    req = request.Request(api_url, data=data, headers=xueqiu_headers(cookie), method="POST")
    status, body, result = read_response(req, stage)
    if stage == "post" and not result.get("id"):
        raise XueqiuError("post_unconfirmed", "雪球未返回帖子 ID，请人工确认，避免重复发帖")
    if stage == "validate" and result.get("success") is not True:
        raise XueqiuError("invalid_response", "雪球文本校验未返回 success=true")
    return status, body


def get_session_token(api_path: str = "/statuses/update.json") -> str:
    cookie = get_xueqiu_cookie()
    if not cookie:
        raise RuntimeError("missing XUEQIU_COOKIE")
    token_url = os.getenv("XUEQIU_SESSION_TOKEN_URL", DEFAULT_TOKEN_URL).strip() or DEFAULT_TOKEN_URL
    url = f"{token_url}?{parse.urlencode({'api_path': api_path})}"
    req = request.Request(url, headers=xueqiu_headers(cookie), method="GET")
    _, _, result = read_response(req, "token")
    token = result.get("session_token", "")
    if not token:
        raise XueqiuError("invalid_response", "xueqiu token response missing session_token")
    return token


def refresh_waf_cookie() -> bool:
    refresh_script = BASE_DIR / "scripts" / "xueqiu_waf_refresh.py"
    if not refresh_script.exists():
        return False
    try:
        proc = subprocess.run(
            [sys.executable, str(refresh_script), "--write-env"],
            cwd=BASE_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=90,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        print("Xueqiu browser refresh failed or timed out", file=sys.stderr)
        return False
    if proc.returncode != 0:
        try:
            result = json.loads(proc.stdout)
        except (ValueError, TypeError):
            print("Xueqiu browser refresh unavailable; check logs/xueqiu_waf_refresh.json", file=sys.stderr)
            return False
        if result.get("reason") in {"verification_required", "cookie_invalid", "account_mismatch"}:
            raise XueqiuError(result["reason"], result.get("detail", "browser refresh failed"))
        return False
    os.environ.pop("XUEQIU_COOKIE", None)
    load_dotenv(BASE_DIR / ".env", override=True)
    return True


def with_recovery(action):
    try:
        return action()
    except XueqiuError as exc:
        if not exc.recoverable or not refresh_waf_cookie():
            raise
    return action()


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
        stage="validate",
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
        get_xueqiu_cookie(),
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

    if not args.validate_only and history_contains(args.trade_date, args.session_label):
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
            def validate():
                get_session_token()
                return validate_text(text)

            status_code, body = with_recovery(validate)
        except Exception as exc:
            row = {**base_row, "status": "validate_failed", "error": str(exc)}
            append_history(row)
            failure_alert(exc, row)
            print(str(exc), file=sys.stderr)
            return 1
        row = {**base_row, "status": "validated", "http_status": status_code, "response": body[:1000]}
        append_history(row)
        print(f"validated Xueqiu text/auth: http {status_code}")
        return 0

    try:
        status_code, body = with_recovery(lambda: post_status(text))
    except Exception as exc:
        row = {**base_row, "status": "failed", "error": str(exc)}
        append_history(row)
        failure_alert(exc, row)
        print(str(exc), file=sys.stderr)
        return 1

    row = {**base_row, "status": "posted", "http_status": status_code, "response": body[:1000]}
    append_history(row)
    clear_alert()
    print(f"posted Xueqiu status: http {status_code}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
