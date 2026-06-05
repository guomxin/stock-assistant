#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
from http.cookies import SimpleCookie
from pathlib import Path

from dotenv import dotenv_values
from playwright.sync_api import sync_playwright


BASE = Path("/mnt/ssd01/stocks")
ENV_PATH = BASE / ".env"
LOG_PATH = BASE / "logs/xueqiu_waf_refresh.json"


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
        if "xueqiu.com" not in domain:
            continue
        merged[row["name"]] = row["value"]
    return "; ".join(f"{name}={morsel.value}" for name, morsel in merged.items())


def write_env_cookie_b64(cookie: str) -> None:
    encoded = base64.b64encode(cookie.encode("utf-8")).decode("ascii")
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    replaced = False
    out = []
    for line in lines:
        if line.startswith("XUEQIU_COOKIE_B64="):
            out.append(f"XUEQIU_COOKIE_B64={encoded}")
            replaced = True
        elif line.startswith("XUEQIU_COOKIE="):
            continue
        else:
            out.append(line)
    if not replaced:
        out.append(f"XUEQIU_COOKIE_B64={encoded}")
    ENV_PATH.write_text("\n".join(out) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write-env", action="store_true")
    args = parser.parse_args()

    old_cookie = load_cookie()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"
            ),
            locale="zh-CN",
            viewport={"width": 1365, "height": 900},
        )
        context.add_cookies(cookie_header_to_list(old_cookie))
        page = context.new_page()
        page.goto("https://xueqiu.com/", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(8000)
        title = page.title()
        text = page.locator("body").inner_text(timeout=10000)[:1000]
        cookies = context.cookies()
        browser.close()

    new_cookie = merge_cookie_header(old_cookie, cookies)
    names = sorted({row["name"] for row in cookies if "xueqiu.com" in row.get("domain", "")})
    payload = {
        "title": title,
        "text_head": text,
        "cookie_names": names,
        "has_acw_sc_v2": "acw_sc__v2" in names,
        "has_waf_page": "aliyun_waf" in text or "_waf_" in text,
        "old_len": len(old_cookie),
        "new_len": len(new_cookie),
    }
    LOG_PATH.parent.mkdir(exist_ok=True)
    LOG_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False))
    if args.write_env:
        write_env_cookie_b64(new_cookie)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
