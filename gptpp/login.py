#!/usr/bin/env python3
"""
Capture a logged-in chatgpt.com session.

Opens https://chatgpt.com/ in Playwright Firefox. Log in normally - the
script snatches the `accessToken` from /api/auth/session (or from the
Authorization bearer headers in browser traffic), grabs cookies, queries
/backend-api/models, and saves everything to .chatgpt_credentials.json.

The browser session (cookies + localStorage) is remembered in
.chatgpt_browser_state.json, so reruns restore it and skip login while
the token stays valid. client.py reuses the same file.

Usage:
    python -m gptpp.login                # headed browser (default)
    python -m gptpp.login --headless     # no window (rarely useful)
    python -m gptpp.login --timeout 900  # wait up to 15 min for login
"""

import argparse
import asyncio
import base64
import json
import os
import sys
import time

from playwright.async_api import async_playwright

AUTH_URL = "https://chatgpt.com/"
MODELS_URL = "https://chatgpt.com/backend-api/models"
CREDENTIALS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".chatgpt_credentials.json")
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".chatgpt_browser_state.json")


def jwt_claims(token: str) -> dict:
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        pad = "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(parts[1] + pad))
    except Exception:
        return {}


def is_guest(token: str) -> bool:
    """ChatGPT anonymous sessions hand out a guest JWT - not what we want."""
    claims = jwt_claims(token)
    if not claims:
        return True
    kind = str(claims.get("kind") or "")
    if kind.lower() in ("anonymous", "guest"):
        return True
    email = str(claims.get("email") or "")
    return email.lower().endswith("chatgpt.com") and not claims.get("id")


async def fetch_models(request, bearer: str) -> tuple[int, list]:
    """Returns (status, model_ids). status 0 = network failure."""
    try:
        resp = await request.get(
            MODELS_URL,
            headers={
                "Authorization": f"Bearer {bearer}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        if not resp.ok:
            return resp.status, []
        data = await resp.json()
        items = data.get("data") if isinstance(data, dict) else data
        if isinstance(items, dict):
            items = items.get("models") or items.get("list") or []
        ids = []
        for m in items or []:
            if isinstance(m, dict):
                mid = m.get("id") or m.get("slug") or m.get("name")
                if mid:
                    ids.append(str(mid))
            elif m:
                ids.append(str(m))
        return resp.status, ids
    except Exception as e:
        print(f"[login] model list fetch failed: {e}")
        return 0, []


async def fetch_session(request, cookie_header: str) -> tuple[str, str]:
    """GET /api/auth/session -> (accessToken, email). Empty on failure."""
    try:
        resp = await request.get(
            "https://chatgpt.com/api/auth/session",
            headers={"Cookie": cookie_header} if cookie_header else {},
        )
        if not resp.ok:
            return "", ""
        data = await resp.json()
        return data.get("accessToken") or "", (
            ((data.get("user") or {}).get("email")) or ""
        )
    except Exception:
        return "", ""


async def login(timeout_s: int = 600, headless: bool = False) -> None:
    async with async_playwright() as p:
        browser = await p.firefox.launch(
            headless=headless,
            firefox_user_prefs={
                "dom.webdriver.enabled": False,
                "useAutomationExtension": False,
            },
        )
        restore_state = STATE_FILE if os.path.exists(STATE_FILE) else None
        if restore_state:
            print(f"[login] Restoring remembered session from {os.path.basename(STATE_FILE)}")
        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
            user_agent="Mozilla/5.0 (X11; Linux x86_64; rv:141.0) Gecko/20100101 Firefox/141.0",
            storage_state=restore_state,
        )
        page = await context.new_page()

        captured = {"bearer": ""}
        rejected: set[str] = set()

        def on_request(req):
            try:
                auth = req.headers.get("authorization", "")
            except Exception:
                auth = ""
            if auth.lower().startswith("bearer "):
                token = auth.split(" ", 1)[1].strip()
                if token and not is_guest(token):
                    captured["bearer"] = token

        page.on("request", on_request)

        await page.goto(AUTH_URL, wait_until="domcontentloaded", timeout=60000)
        print(f"[login] Opened {AUTH_URL}")
        print("[login] Waiting for a valid logged-in token (log in if the page asks)...")

        token = ""
        email = ""
        models: list = []
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                cookies = await context.cookies("https://chatgpt.com")
            except Exception:
                cookies = []
            cookie_header = "; ".join(f"{c['name']}={c['value']}" for c in cookies)

            sess_token, sess_email = await fetch_session(page.request, cookie_header)
            candidate = sess_token or captured["bearer"]
            if candidate and not is_guest(candidate):
                if candidate in rejected:
                    await asyncio.sleep(0.5)
                    continue
                status, model_ids = await fetch_models(page.request, candidate)
                if status in (401, 403):
                    print(f"[login] Stored session expired ({status}) — log in again in the window")
                    rejected.add(candidate)
                    captured["bearer"] = ""
                    await asyncio.sleep(0.5)
                    continue
                token = candidate
                email = sess_email
                models = model_ids
                break

            await asyncio.sleep(0.75)

        if not token:
            await browser.close()
            raise SystemExit(f"[login] Timed out after {timeout_s}s — no valid logged-in token seen.")

        claims = jwt_claims(token)
        email = email or str(claims.get("email") or "")
        app_meta = claims.get("https://api.openai.com/auth")
        if not isinstance(app_meta, dict):
            app_meta = {}
        user_id = str(app_meta.get("chatgpt_account_id") or claims.get("sub") or claims.get("id") or "")

        cookies = await context.cookies("https://chatgpt.com")
        cookie_header = "; ".join(f"{c['name']}={c['value']}" for c in cookies)

        try:
            await context.storage_state(path=STATE_FILE)
            print(f"[login] Browser session remembered in {os.path.basename(STATE_FILE)}")
        except OSError as e:
            print(f"[login] Could not save browser state: {e}")

        await browser.close()

    existing = {}
    if os.path.exists(CREDENTIALS_FILE):
        try:
            with open(CREDENTIALS_FILE) as f:
                existing = json.load(f)
        except (json.JSONDecodeError, OSError):
            existing = {}

    creds = {
        **existing,
        "token": token,
        "cookie": cookie_header or existing.get("cookie", ""),
        "logged_in": True,
        "email": email,
        "user_id": user_id,
        "models": models,
        "logged_in_at": int(time.time()),
    }
    with open(CREDENTIALS_FILE, "w") as f:
        json.dump(creds, f, indent=2)

    print(f"[login] Logged in as {email or user_id or token[:24] + '...'}")
    if models:
        print(f"[login] Models from /backend-api/models: {', '.join(models)}")
    else:
        print("[login] Model list unavailable (endpoint may require additional auth)")
    print(f"[login] Credentials saved to {CREDENTIALS_FILE}")


def main():
    ap = argparse.ArgumentParser(description="Capture logged-in chatgpt.com credentials")
    ap.add_argument("--timeout", type=int, default=600, help="seconds to wait for login (default 600)")
    ap.add_argument("--headless", action="store_true", help="run without a browser window")
    args = ap.parse_args()
    try:
        asyncio.run(login(timeout_s=args.timeout, headless=args.headless))
    except KeyboardInterrupt:
        sys.exit("\n[login] Cancelled.")


if __name__ == "__main__":
    main()
