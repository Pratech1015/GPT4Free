#!/usr/bin/env python3
"""Turnstile solver: runs the extracted Sentinel VM through Node.js.

The chatgpt.com mobile web app ships an obfuscated JavaScript VM (the
``turnstile`` program from ``chat-requirements/prepare``) whose result is
XORed against the very fingerprint blob ``p`` that was sent to prepare.
``gptpp/turnstile.mjs`` is that VM rebuilt for Node with a Firefox-shaped
environment shim, so the emitted token is byte-compatible with what the
real browser produces.

Requires ``node`` (>= 18) on PATH. Without it every challenge raises
:class:`gptpp.errors.TurnstileRequiredError`.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import os
import re
from typing import Any, Dict

from .errors import TurnstileRequiredError

_DIR = os.path.dirname(os.path.abspath(__file__))
_CLI = os.path.join(_DIR, "_solve_cli.mjs")


def _parse(output: bytes) -> Dict[str, Any]:
    try:
        return json.loads(output.decode("utf-8", "replace") or "{}")
    except json.JSONDecodeError:
        return {"error": f"unparseable solver output: {output[:200]!r}"}


async def solve_turnstile(p: str, dx: str, timeout: float = 30.0) -> str:
    """Solve a Turnstile challenge and return the token for finalize.

    ``p`` must be the exact fingerprint blob sent to prepare (it doubles as
    the VM's XOR key) and ``dx`` the challenge program from the response.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "node",
            _CLI,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as e:
        raise TurnstileRequiredError(
            "Turnstile challenge requires the Node.js runtime (node >= 18 on PATH)"
        ) from e

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(json.dumps({"p": p, "dx": dx}).encode("utf-8")),
            timeout=timeout,
        )
    except asyncio.TimeoutError as e:
        proc.kill()
        raise TurnstileRequiredError("Turnstile solver timed out") from e

    data = _parse(stdout)
    token = str(data.get("token") or "")
    failure = ""
    if not token or token.startswith("TIMEOUT"):
        failure = data.get("error") or token or stderr.decode("utf-8", "replace")
    else:
        try:
            decoded = base64.b64decode(token).decode("utf-8", "replace")
        except (ValueError, binascii.Error):
            decoded = ""
        if re.match(r"^\d+: ", decoded):
            failure = decoded
    if failure:
        raise TurnstileRequiredError(f"Turnstile solver failed: {str(failure)[:400]}")
    return token
