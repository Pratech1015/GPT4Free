#!/usr/bin/env python3
"""
Browser-free ChatGPT sentinel flow.

Mirrors what the chatgpt.com frontend does before every completion:

  1. build a client fingerprint blob  ->  "gAAAAAC" + base64(JSON(config))
  2. POST /backend-api/sentinel/chat-requirements/prepare   {"p": <blob>}
  3. solve the proof-of-work the prepare step asks for       (FNV-1a hex sweep)
  4. POST /backend-api/sentinel/chat-requirements/finalize   {"prepare_token", "proofofwork"}
  5. use the returned `token` as the OpenAI-Sentinel-Chat-Requirements-Token header

The fingerprint config is the exact 25-slot array the frontend's
`getConfig()` builds (screen, date string, UA, script src, data-build,
language, random window/document keys, performance.now, sid, timeOrigin,
feature flags ...). Slots 3 and 9 are overwritten by the attempt counter and
elapsed ms while solving, exactly like `_runCheck()`.

Turnstile challenges are solved by `gptpp.solver` (the extracted challenge
VM running on Node.js); `TurnstileRequiredError` surfaces only when the
runtime or the solve itself fails.
"""

from __future__ import annotations

import base64
import json
import random
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import aiohttp

from .errors import TurnstileRequiredError, UpstreamError

REQUIREMENTS_PREFIX = "gAAAAAC"   # fingerprint blob
PROOF_PREFIX = "gAAAAAB"          # proof-of-work answer

MAX_POW_ATTEMPTS = 500_000
REQUIREMENTS_TTL = 8 * 60         # frontend expires chat-requirements after 9 min

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64; rv:141.0) Gecko/20100101 Firefox/141.0"

BASE_URL = "https://chatgpt.com"
API_URL = "https://chatgpt.com/backend-api"

_TZ_UTC_NAME = "Coordinated Universal Time"


def _js_b64(obj) -> str:
    """btoa(JSON.stringify(obj)) - same bytes the frontend ships."""
    raw = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def fnv_hex(seed: str) -> str:
    """The frontend's GXt(): FNV-1a then a murmur-style fmix32, hex padded."""
    h = 2166136261
    for ch in seed:
        h ^= ord(ch)
        h = (h * 16777619) & 0xFFFFFFFF
    h ^= h >> 16
    h = (h * 2246822507) & 0xFFFFFFFF
    h ^= h >> 13
    h = (h * 3266489909) & 0xFFFFFFFF
    h ^= h >> 16
    return format(h & 0xFFFFFFFF, "08x")


def js_date_string(when: Optional[datetime] = None) -> str:
    """`"" + new Date` - e.g. 'Sat Oct 03 2026 17:32:08 GMT+0000 (…)'."""
    when = when or datetime.now().astimezone()
    offset = when.strftime("%z") or "+0000"
    tzname = when.tzname() or ""
    if offset == "+0000":
        tzname = _TZ_UTC_NAME
    elif not tzname:
        tzname = "Coordinated Universal Time"
    return f"{when.strftime('%a %b %d %Y %H:%M:%S')} GMT{offset} ({tzname})"


@dataclass
class BrowserProfile:
    """The slice of browser state that lands in the fingerprint blob."""

    user_agent: str = USER_AGENT
    screen_width: int = 1366
    screen_height: int = 768
    language: str = "en-US"
    languages: str = "en-US,en"
    hardware_concurrency: int = 8
    script_src: str = "https://chatgpt.com/cdn/assets/manifest-8b7f218b.js"
    data_build: str = "prod-e0e6637ebf1dbe024371aece17ddd0057b5734fa"
    document_key: str = "body"
    window_key: str = "origin"
    nav_key: str = "platform"
    nav_value: str = "Linux x86_64"
    # Firefox has no performance.memory -> JSON `null`
    js_heap_size_limit: Optional[int] = None
    install_trigger: bool = True          # Number('InstallTrigger' in window)
    performance_now: float = field(default_factory=lambda: round(random.uniform(800, 24000), 3))
    sid: str = field(default_factory=lambda: str(uuid.uuid4()))
    search_keys: str = ""
    now: datetime = field(default_factory=lambda: datetime.now().astimezone())

    def build_config(self, attempt: int = 1, elapsed_ms: int = 0) -> list:
        return [
            self.screen_width + self.screen_height,
            js_date_string(self.now),
            self.js_heap_size_limit,
            attempt,
            self.user_agent,
            self.script_src,
            self.data_build,
            self.language,
            self.languages,
            elapsed_ms,
            f"{self.nav_key}\u2212{self.nav_value}",
            self.document_key,
            self.window_key,
            self.performance_now,
            self.sid,
            self.search_keys,
            self.hardware_concurrency,
            round(time.time() * 1000, 3),
            0, 0, 0, 0, 0, 0,
            1 if self.install_trigger else 0,
        ]


def build_requirements_blob(profile: Optional[BrowserProfile] = None) -> str:
    profile = profile or BrowserProfile()
    return REQUIREMENTS_PREFIX + _js_b64(profile.build_config(attempt=1, elapsed_ms=0))


def solve_pow(seed: str, difficulty: str, profile: Optional[BrowserProfile] = None,
              max_attempts: int = MAX_POW_ATTEMPTS, deadline: Optional[float] = None) -> Optional[str]:
    """
    Sweep nonces until fnv(seed + b64(config)) has a hex prefix <= difficulty.

    Returns the raw answer (b64 + '~S'); the caller prefixes it with gAAAAAB.
    """
    profile = profile or BrowserProfile()
    base = profile.build_config(attempt=0, elapsed_ms=0)
    diff = difficulty or ""
    diff_len = len(diff)
    started = time.time()
    for attempt in range(max_attempts):
        if deadline is not None and time.time() > deadline:
            return None
        cfg = list(base)
        cfg[3] = attempt
        cfg[9] = int(round((time.time() - started) * 1000))
        answer = _js_b64(cfg)
        if fnv_hex(seed + answer)[:diff_len] <= diff:
            return answer + "~S"
        if attempt and attempt % 20000 == 0:
            # keep the elapsed slot moving like the frontend's performance.now()
            time.sleep(0)
    return None


@dataclass
class ChatRequirements:
    token: str
    prepare_token: str = ""
    persona: str = ""
    turnstile_required: bool = False
    proofofwork_required: bool = False
    raw: dict = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    @property
    def expires_at(self) -> float:
        return self.created_at + REQUIREMENTS_TTL

    @property
    def expired(self) -> bool:
        return time.time() >= self.expires_at

    def headers(self, turnstile_token: str = "", proof_token: str = "",
                sentinel_token: str = "") -> dict:
        out = {"OAI-Telemetry": "[1,null]"}
        if self.token:
            out["OpenAI-Sentinel-Chat-Requirements-Token"] = self.token
        if turnstile_token:
            out["OpenAI-Sentinel-Turnstile-Token"] = turnstile_token
        if proof_token:
            out["OpenAI-Sentinel-Proof-Token"] = proof_token
        if sentinel_token:
            out["OpenAI-Sentinel-Token"] = sentinel_token
        return out


async def get_chat_requirements(
    session: aiohttp.ClientSession,
    *,
    profile: Optional[BrowserProfile] = None,
    turnstile_token: str = "",
    timeout: float = 60.0,
) -> ChatRequirements:
    """
    Run prepare -> proof-of-work -> finalize over plain HTTP.

    Raises TurnstileRequiredError when the site demands a Turnstile challenge
    we cannot solve without a browser, UpstreamError on anything else.
    """
    profile = profile or BrowserProfile()
    blob = build_requirements_blob(profile)
    ct = aiohttp.ClientTimeout(total=timeout)

    async with session.post(
        f"{API_URL}/sentinel/chat-requirements/prepare",
        data=json.dumps({"p": blob}, separators=(",", ":")),
        timeout=ct,
    ) as resp:
        text = await resp.text()
        if resp.status != 200:
            raise UpstreamError(f"chat-requirements prepare failed {resp.status}: {text[:400]}",
                                status=resp.status, body=text)
        prepare = json.loads(text)

    body: dict = {"prepare_token": prepare.get("prepare_token", "")}
    pow_spec = prepare.get("proofofwork") or {}
    proof_token = ""
    if pow_spec.get("required") and pow_spec.get("seed") and pow_spec.get("difficulty"):
        import asyncio
        answer = await asyncio.to_thread(
            solve_pow,
            pow_spec["seed"],
            str(pow_spec["difficulty"]),
            profile,
            MAX_POW_ATTEMPTS,
            time.time() + 30,
        )
        if not answer:
            raise UpstreamError("proof-of-work did not converge before the deadline")
        proof_token = PROOF_PREFIX + answer
        body["proofofwork"] = proof_token

    turnstile = prepare.get("turnstile") or {}
    turnstile_required = bool(turnstile.get("required"))
    if turnstile_token:
        body["turnstile"] = turnstile_token
    elif turnstile_required and not turnstile.get("dx"):
        # required with no challenge payload -> nothing sane to send
        raise TurnstileRequiredError(
            "chat-requirements requested a Turnstile challenge; "
            "solve it in a real browser (gptpp.client) or retry later"
        )

    async with session.post(
        f"{API_URL}/sentinel/chat-requirements/finalize",
        data=json.dumps(body, separators=(",", ":")),
        timeout=ct,
    ) as resp:
        text = await resp.text()
        if resp.status != 200:
            raise UpstreamError(f"chat-requirements finalize failed {resp.status}: {text[:400]}",
                                status=resp.status, body=text)
        final = json.loads(text)

    token = final.get("token") or ""
    if not token:
        raise UpstreamError(f"chat-requirements returned no token: {text[:400]}")

    merged = {**prepare, **final}
    return ChatRequirements(
        token=token,
        prepare_token=prepare.get("prepare_token", ""),
        persona=str(final.get("persona") or prepare.get("persona") or ""),
        turnstile_required=turnstile_required,
        proofofwork_required=bool(pow_spec.get("required")),
        raw=merged,
    )
