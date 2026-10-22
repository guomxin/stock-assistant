#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
from datetime import datetime
from http.cookies import SimpleCookie
from pathlib import Path
from urllib import request
from urllib.error import URLError

from dotenv import dotenv_values

from post_xueqiu_status import parse_response, perform_session_request, save_cookie, save_http_session, session_cookie_header, token_from_cookie, xueqiu_headers


BASE = Path(__file__).resolve().parents[1]
ENV_PATH = BASE / ".env"
LOG_PATH = BASE / "logs/xueqiu_waf_refresh.json"
PROFILE_PATH = BASE / "run/xueqiu_chrome_profile"


def load_cookie() -> str:
    env = dotenv_values(ENV_PATH)
    if env.get("XUEQIU_COOKIE"):
        return env["XUEQIU_COOKIE"].strip()
    return base64.b64decode(env.get("XUEQIU_COOKIE_B64", "")).decode("utf-8").strip()


def cookie_header_to_list(header: str) -> list[dict]:
    parsed = SimpleCookie()
    parsed.load(header)
    out = []
    for name, morsel in parsed.items():
        out.append(
            {
                "name": name,
                "value": morsel.value,
                "domain": ".xueqiu.com",
                "path": "/",
                "httpOnly": False,
                "secure": True,
                "sameSite": "Lax",
            }
        )
    return out


def merge_cookie_header(old_header: str, browser_cookies: list[dict]) -> str:
    merged = SimpleCookie()
    merged.load(old_header)
    for row in browser_cookies:
        domain = row.get("domain", "")
        if domain.lstrip(".") != "xueqiu.com" and not domain.endswith(".xueqiu.com"):
            continue
        merged[row["name"]] = row["value"]
    return "; ".join(f"{name}={morsel.value}" for name, morsel in merged.items())


def refresh(write_env: bool) -> dict:
    from playwright.sync_api import sync_playwright

    old_cookie = load_cookie()
    user_agent = dotenv_values(ENV_PATH).get("XUEQIU_USER_AGENT")
    expected_user = token_from_cookie(old_cookie, "u")
    cdp_url = "http://127.0.0.1:9223"
    try:
        with request.urlopen(cdp_url + "/json/version", timeout=2):
            attached = True
    except (URLError, OSError):
        attached = False

    with sync_playwright() as pw:
        if attached:
            browser = pw.chromium.connect_over_cdp(cdp_url, timeout=10000)
            context = browser.contexts[0]
        else:
            PROFILE_PATH.mkdir(parents=True, exist_ok=True)
            context = pw.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_PATH),
                channel="chrome",
                headless=True,
                locale="zh-CN",
                viewport={"width": 1365, "height": 900},
                **({"user_agent": user_agent} if user_agent else {}),
            )
        page = None
        try:
            existing = context.cookies()
            existing_header = session_cookie_header(existing)
            profile_user = token_from_cookie(existing_header, "u")
            if expected_user and profile_user and profile_user != expected_user:
                return {"ok": False, "reason": "account_mismatch", "detail": "雪球浏览器账号与配置不一致，请人工确认"}
            # Never overwrite a remembered browser session with stale HTTP tokens.
            if not token_from_cookie(existing_header, "xq_a_token"):
                context.add_cookies(cookie_header_to_list(old_cookie))
            page = context.new_page()
            page.goto("https://xueqiu.com/", wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(3000)
            title = page.title()
            text = page.locator("body").inner_text(timeout=5000)
            if "check.xueqiu.com" in page.url or any(s in title + text for s in ("访问触发保护", "请完成人机验证", "aliyun_waf", "_waf_")):
                return {"ok": False, "reason": "verification_required", "detail": "雪球触发访问保护，请在浏览器完成人机验证"}
            # Cookie presence alone does not prove the session is still accepted.
            browser_cookie = session_cookie_header(context.cookies())
            headers = xueqiu_headers(browser_cookie)
            headers.pop("Cookie", None)
            headers["User-Agent"] = page.evaluate("navigator.userAgent")
            response = context.request.get(
                "https://xueqiu.com/provider/session/token.json?api_path=%2Fstatuses%2Fupdate.json",
                headers=headers,
                timeout=15000,
            )
            result = parse_response(response.text(), "token", response.status)
            if not result.get("session_token"):
                return {"ok": False, "reason": "cookie_invalid", "detail": "浏览器登录态未恢复，请重新登录雪球"}
            cookies = context.cookies()
            new_cookie = session_cookie_header(cookies)
            user = token_from_cookie(new_cookie, "u")
            if expected_user and user != expected_user:
                return {"ok": False, "reason": "account_mismatch", "detail": "刷新后的雪球账号与配置不一致，请人工确认"}
            if not token_from_cookie(new_cookie, "xq_a_token"):
                return {"ok": False, "reason": "cookie_invalid", "detail": "浏览器缺少雪球登录凭据，请重新登录"}
            if write_env:
                user_agent = page.evaluate("navigator.userAgent")
                req = request.Request(
                    "https://xueqiu.com/provider/session/token.json?api_path=%2Fstatuses%2Fupdate.json",
                    headers=xueqiu_headers(new_cookie),
                )
                status, body, cookies = perform_session_request(req, {"cookies": cookies, "user_agent": user_agent}, playwright=pw)
                result = parse_response(body, "token", status)
                if not result.get("session_token"):
                    return {"ok": False, "reason": "cookie_invalid", "detail": "正式 HTTP 链路校验未通过"}
                save_http_session(cookies, user_agent, user)
                save_cookie(session_cookie_header(cookies), user_agent)
            return {"ok": True, "source": "open_browser" if attached else "persistent_profile", "cookie_count": len(cookies), "env_updated": write_env, "http_session_saved": write_env, "production_transport_checked": write_env}
        finally:
            if page is not None:
                page.close()
            if not attached:
                context.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write-env", action="store_true")
    args = parser.parse_args()

    try:
        payload = refresh(args.write_env)
    except ImportError:
        payload = {"ok": False, "reason": "dependency_missing", "detail": "请安装 requirements.txt 中的 Playwright；刷新使用本机 Google Chrome"}
    except Exception as exc:
        # Browser exceptions can contain page source; keep credentials out of logs.
        payload = {"ok": False, "reason": getattr(exc, "reason", "refresh_failed"), "detail": "雪球浏览器刷新未恢复，请检查登录或验证状态", "error_type": type(exc).__name__}
    payload["checked_at"] = datetime.now().isoformat(timespec="seconds")
    LOG_PATH.parent.mkdir(exist_ok=True)
    LOG_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
