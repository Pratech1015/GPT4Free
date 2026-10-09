"""
Route chatgpt.com requests through an external upstream relay.

Some host IPs are challenged by chatgpt.com's Cloudflare (403 HTML page).
A relay - any HTTP reverse proxy or edge function that egresses the
request from a trusted IP - can be configured with:

    GPTPP_UPSTREAM=https://relay.example/<prefix> python3 ...

`via()` rewrites every chatgpt.com URL at the request site; cookie
operations must use `base()` so the jar follows the relay origin. The
value may include a path prefix (e.g. a shared secret the relay checks)
and everything before the first chatgpt.com path segment is preserved.
URLs that are not chatgpt.com (attachment hosts, presigned uploads) pass
through untouched. The setting is read once at import time; use
`set_upstream()` to change it later.
"""

import os

import aiohttp
from yarl import URL

CHATGPT = "https://chatgpt.com"

_upstream = os.environ.get("GPTPP_UPSTREAM", "").rstrip("/")


def set_upstream(url: str = "") -> None:
    """Configure (or clear, with an empty url) the relay at runtime."""
    global _upstream
    _upstream = (url or "").rstrip("/")


def upstream() -> str:
    return _upstream


def via(url: str) -> str:
    """Rewrite a chatgpt.com URL through the relay when one is set."""
    if _upstream and url.startswith(CHATGPT):
        return _upstream + url[len(CHATGPT):]
    return url


def base() -> str:
    """Origin that owns cookies: the relay, or chatgpt.com directly."""
    return _upstream or CHATGPT


def cookie_jar() -> "aiohttp.CookieJar":
    """Cookie jar configured for the active origin.

    A relay is often an ``http://`` origin, which aiohttp's default jar
    rejects twice over: it will not *store* cookies for IP hosts and will
    not *send* ``Secure`` cookies over a non-TLS scheme. ``unsafe`` plus
    ``treat_as_secure_origin`` fix both while direct chatgpt.com behavior
    stays on aiohttp defaults.
    """
    if not _upstream:
        return aiohttp.CookieJar()
    return aiohttp.CookieJar(
        unsafe=True, treat_as_secure_origin=URL(_upstream).origin()
    )
