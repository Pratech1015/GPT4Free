#!/usr/bin/env python3
"""
Pure-HTTP chatgpt.com transport - no browser, no Turnstile.

Logged-out chatgpt.com serves the "web mobile" app, which completes through
`POST /unauth-mweb/conversation/updates` instead of the sentinel-hardened
`/backend-api/conversation`. The flow per turn:

  1. warm-up GET /                         first-party cookies (cf_bm, oai-did ...)
  2. /unauth-mweb/sentinel/chat-requirements prepare -> PoW -> finalize
  3. /unauth-mweb/conversation/prepare     -> conduit token (x-conduit-token)
  4. /unauth-mweb/conversation/updates     -> stream of declarative-partial-update
                                              HTML frames (`text/vnd.openai.web-
                                              mobile-partial+html`)

The stream is parsed incrementally so `send_message_stream` yields deltas as
they arrive. Conversation continuation mirrors the browser: the
`data-conversation-state` JSON on `message-stream-complete` is exactly what the
next turn sends back (`backendConversationId`, `parentMessageId`,
`userMessageCount`).
"""

from __future__ import annotations

import asyncio
import codecs
import html
import json
import re
import time
import uuid
from typing import AsyncGenerator, Dict, List, Optional, Tuple

import aiohttp
from yarl import URL

from .errors import AuthError, TurnstileRequiredError, UpstreamError
from .sentinel import (
    BASE_URL,
    MAX_POW_ATTEMPTS,
    PROOF_PREFIX,
    USER_AGENT,
    BrowserProfile,
    build_requirements_blob,
    solve_pow,
)

MW_URL = f"{BASE_URL}/unauth-mweb"
UPDATES_URL = f"{MW_URL}/conversation/updates"
PREPARE_URL = f"{MW_URL}/conversation/prepare"
SENTINEL_URL = f"{MW_URL}/sentinel/chat-requirements"

DPU_CONTENT_TYPE = "text/vnd.openai.web-mobile-partial+html"
FORM_CT = "application/x-www-form-urlencoded;charset=UTF-8"
JSON_CT = "application/json;charset=UTF-8"

_CLIENT_CONTEXT = {
    "app_name": "chatgpt.com",
    "has_web_push_capabilities": True,
    "is_dark_mode": False,
    "web_push_notification_permission": "default",
    "page_height": 900,
    "page_width": 1600,
    "pixel_ratio": 1,
    "screen_height": 768,
    "screen_width": 1366,
    "time_since_loaded": 4,
}

_PI_RE = re.compile(r"<\?(?:start|end|marker)[^>]*>", re.I)
_BR_RE = re.compile(r"<br\s*/?>", re.I)
_LI_OPEN_RE = re.compile(r"<li\b[^>]*>", re.I)
_BLOCK_CLOSE_RE = re.compile(
    r"</(?:p|div|li|h[1-6]|blockquote|pre|section|tr)\b[^>]*>", re.I
)
_WRAPPER_RE = re.compile(
    r"</?(?:ul|ol|table|thead|tbody|section|article|figure|h[1-6])\b[^>]*>", re.I
)
_TAG_RE = re.compile(r"<[^>]+>")
_WRITING_BLOCK_RE = re.compile(r'data-writing-block-source="([^"]*)"')
_STREAM_BLOCK_RE = re.compile(
    r'<(\w+)[^>]*data-assistant-stream-block-index="(\d+)"[^>]*>(.*?)</\1>',
    re.S | re.I,
)
_CONTROL_RE = re.compile(r'<span\b[^>]*data-conversation-control="([^"]+)"([^>]*)>', re.I)
_ATTR_RE = re.compile(r'data-([a-z-]+)="([^"]*)"')
_ATTR_ANY_RE = re.compile(r'([a-zA-Z][\w-]*)="([^"]*)"')
_DOC_CONTROL_RE = re.compile(
    r'data-conversation-control="([^"]+)"((?:\s+data-[a-z-]+="[^"]*")+)', re.I
)


def _client_context() -> str:
    return json.dumps(_CLIENT_CONTEXT, separators=(",", ":"))


def _offset_minutes() -> int:
    off = time.localtime().tm_gmtoff
    return int(off // 60)


def _content_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict)
            and part.get("type") in ("text", "input_text", None)
            and isinstance(part.get("text"), str)
        )
    return str(content or "")


def flatten_messages(messages: list) -> str:
    """Transcript-style prompt for a brand-new conversation."""
    chunks: list[str] = []
    system_bits: list[str] = []
    for msg in messages:
        role = msg.get("role", "user")
        text = _content_text(msg.get("content", ""))
        if not text:
            continue
        if role in ("system", "developer"):
            system_bits.append(text)
        else:
            label = "User" if role == "user" else "Assistant"
            chunks.append(f"{label}: {text}")
    out = ""
    if system_bits:
        out = "System instruction: " + "\n".join(system_bits) + "\n\n"
    if chunks:
        out += "\n".join(chunks) + "\n\nAssistant:"
    return out.strip() or "Hello"


def strip_markup(fragment: str) -> str:
    """
    Rendered DPU markup -> markdown-ish plain text.

    Lists become `- item`, block boundaries become blank lines, everything
    else (classes, styles, markers) disappears.
    """
    text = _PI_RE.sub("", fragment)
    text = _LI_OPEN_RE.sub("- ", text)
    text = _BLOCK_CLOSE_RE.sub("\n\n", text)
    text = _WRAPPER_RE.sub("\n", text)
    text = _BR_RE.sub("\n", text)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"\n\n(?=- )", "\n", text)
    return text.strip("\n")


def extract_assistant_text(content: str) -> str:
    """
    Pull assistant text out of one DPU frame.

    Live/committed blocks arrive as `<tag data-assistant-stream-block-index="N">`
    elements (paragraphs, lists, headings); older frames carry a
    `data-writing-block-source` JSON attribute instead. The stream markup is
    preferred so streaming and committed frames stay textually identical.
    """
    if not content:
        return ""
    blocks = _STREAM_BLOCK_RE.findall(content)
    if blocks:
        blocks.sort(key=lambda pair: int(pair[1]))
        parts = []
        for _, _, inner in blocks:
            text = strip_markup(inner)
            if text:
                parts.append(text)
        if parts:
            return "\n\n".join(parts)

    sources = []
    for m in _WRITING_BLOCK_RE.finditer(content):
        try:
            payload = json.loads(html.unescape(m.group(1)))
        except (json.JSONDecodeError, ValueError, TypeError):
            continue
        if isinstance(payload, dict) and payload.get("content"):
            try:
                index = int(payload.get("index") or 0)
            except (TypeError, ValueError):
                index = 0
            sources.append((index, str(payload["content"])))
    if sources:
        sources.sort(key=lambda pair: pair[0])
        return "".join(text for _, text in sources)

    return strip_markup(content)


_ASSISTANT_SUFFIX_RE = re.compile(r"-(?:pending|committed(?:-[\w-]+)?)$")


def assistant_group(name: str) -> str:
    """
    `assistant-pending-<uuid>-committed-tail` -> `assistant-pending-<uuid>`.

    The pending (streaming) region and the committed region of one message
    share a group id; whatever the server has committed wins over what is
    still pending.
    """
    return _ASSISTANT_SUFFIX_RE.sub("", name)


class DpuParser:
    """
    Incremental parser for `text/vnd.openai.web-mobile-partial+html` streams.

    feed() takes decoded text and returns events:

        {"kind": "delta",   "delta": str}
        {"kind": "control", "name": str, "attrs": dict}
        {"kind": "done"}
        {"kind": "error",   "message": str, "failure": dict}
    """

    _FRAME_START = '<template data-web-mobile-dpu-frame="'
    _FRAME_RE = re.compile(r'<template data-web-mobile-dpu-frame="\d+">')
    _DIRECTIVE_RE = re.compile(r"<template\b([^>]*)>")
    _OPEN_RE = re.compile(r"<template\b[^>]*>")
    _CLOSE = "</template>"

    def __init__(self) -> None:
        self.buffer = ""
        self.pending: Dict[str, str] = {}    # streaming region text
        self.committed: Dict[str, str] = {}  # committed region text
        self.group_order: List[str] = []
        self.group_keys: Dict[str, List[str]] = {}
        self.emitted = ""
        self.conversation_id = ""
        self.conversation_state: dict = {}
        self.message_id = ""
        self.done = False          # "complete" seen
        self.terminated = False    # "terminal-received" / "message-stream-complete"
        self.failure: Optional[dict] = None
        self.controls: List[str] = []

    # ------------------------------------------------------------------
    def feed(self, data: str) -> list[dict]:
        self.buffer += data
        events: list[dict] = []
        while True:
            start = self.buffer.find(self._FRAME_START)
            if start < 0:
                keep = len(self._FRAME_START) - 1
                self.buffer = self.buffer[-keep:] if keep else ""
                break
            if start:
                self.buffer = self.buffer[start:]
            end = self._scan_frame_end(self.buffer)
            if end is None:
                break
            frame = self.buffer[:end]
            self.buffer = self.buffer[end:]
            events.extend(self._parse_frame(frame))
        return events

    def _scan_frame_end(self, text: str) -> Optional[int]:
        depth = 0
        pos = 0
        while True:
            open_at = text.find("<template", pos)
            close_at = text.find("</template>", pos)
            if close_at < 0:
                return None
            if 0 <= open_at < close_at:
                depth += 1
                pos = open_at + len("<template")
                continue
            depth -= 1
            pos = close_at + len("</template>")
            if depth <= 0:
                return pos

    def _directive_content(self, text: str, start: int) -> str:
        """Content of one nested <template for=...> up to its matching close."""
        depth = 1
        pos = start
        while True:
            open_match = self._OPEN_RE.search(text, pos)
            close_at = text.find(self._CLOSE, pos)
            if close_at < 0:
                return text[start:]
            if open_match and open_match.start() < close_at:
                depth += 1
                pos = open_match.end()
                continue
            depth -= 1
            if depth == 0:
                return text[start:close_at]
            pos = close_at + len(self._CLOSE)

    # ------------------------------------------------------------------
    def _parse_frame(self, frame: str) -> list[dict]:
        match = self._FRAME_RE.match(frame)
        if not match:
            return []
        region = frame[match.end():]

        events: list[dict] = []
        seen: set = set()
        # a frame may nest directives (control span wrapping an assistant
        # template) - walk every <template for=... apply=...> and scope its
        # own content
        for directive in self._DIRECTIVE_RE.finditer(region):
            attrs = dict(_ATTR_ANY_RE.findall(directive.group(1)))
            name = attrs.get("for")
            apply = attrs.get("data-web-mobile-dpu-apply")
            if not name or not apply:
                continue
            content = self._directive_content(region, directive.end())
            for event in self._controls(content):
                key = (event["name"], json.dumps(event["attrs"], sort_keys=True))
                if key in seen:
                    continue
                seen.add(key)
                events.append(event)
            if "assistant" in name:
                text = extract_assistant_text(content)
                if text:
                    events.extend(self._apply_text(name, apply, text))
        if self.failure:
            events.append({
                "kind": "error",
                "message": (
                    "conversation update failed: "
                    f"{self.failure.get('reason') or 'unknown'}"
                    f" (status {self.failure.get('status') or '?'})"
                ),
                "failure": dict(self.failure),
            })
        elif self.done:
            events.append({"kind": "done"})
        return events

    def _controls(self, content: str) -> list[dict]:
        events: list[dict] = []
        matches = list(_CONTROL_RE.finditer(content))
        if not matches:
            matches = list(_DOC_CONTROL_RE.finditer(content))
        for match in matches:
            name = match.group(1)
            attrs = dict(_ATTR_RE.findall(match.group(2)))
            self.controls.append(name)
            if name == "conversation-id":
                if attrs.get("conversation-id"):
                    self.conversation_id = attrs["conversation-id"]
            elif name == "message-stream-complete":
                if attrs.get("conversation-id"):
                    self.conversation_id = attrs["conversation-id"]
                self.terminated = True
                if attrs.get("message-id"):
                    self.message_id = attrs["message-id"]
                self._adopt_state(attrs.get("conversation-state"))
            elif name in ("terminal-received", "complete"):
                self.terminated = True
                if attrs.get("message-id"):
                    self.message_id = attrs["message-id"]
                self._adopt_state(attrs.get("conversation-state"))
                if name == "complete":
                    self.done = True
            elif name == "failed":
                self.failure = {
                    "reason": attrs.get("failure-reason") or "",
                    "status": attrs.get("failure-status") or "",
                    "origin": attrs.get("failure-origin") or "",
                }
            events.append({"kind": "control", "name": name, "attrs": attrs})
        return events

    def _adopt_state(self, raw: str) -> None:
        if not raw:
            return
        try:
            state = json.loads(html.unescape(raw))
        except (json.JSONDecodeError, ValueError):
            return
        if isinstance(state, dict) and state:
            self.conversation_state = state

    # ------------------------------------------------------------------
    def _apply_text(self, name: str, apply: str, text: str) -> list[dict]:
        group = assistant_group(name)
        suffix = name[len(group):]
        store = self.committed if "-committed" in suffix else self.pending
        previous = store.get(name, "")
        updated = previous + text if apply == "append" else text
        if updated != previous:
            store[name] = updated
        if group not in self.group_order:
            self.group_order.append(group)
            self.group_keys[group] = []
        if name not in self.group_keys[group]:
            self.group_keys[group].append(name)

        full = self.answer
        if full == self.emitted:
            return []
        common = 0
        limit = min(len(full), len(self.emitted))
        while common < limit and full[common] == self.emitted[common]:
            common += 1
        delta = full[common:]
        self.emitted = full
        return [{"kind": "delta", "delta": delta}] if delta else []

    # ------------------------------------------------------------------
    @property
    def answer(self) -> str:
        """Final text: committed region wins over the still-pending one."""
        out: List[str] = []
        for group in self.group_order:
            keys = self.group_keys[group]
            committed_keys = [k for k in keys if k in self.committed]
            if committed_keys:
                out.extend(self.committed[k] for k in committed_keys)
            else:
                out.extend(self.pending[k] for k in keys if k in self.pending)
        return "".join(out)


class MwebChatClient:
    """Pure-HTTP chatgpt.com client built on `/unauth-mweb/conversation/updates`."""

    def __init__(
        self,
        session: Optional[aiohttp.ClientSession] = None,
        profile: Optional[BrowserProfile] = None,
    ):
        self._session = session
        self._own_session = session is None
        self._profile = profile or BrowserProfile()
        self._session_id = str(uuid.uuid4())
        self._conv_state: dict = {
            "messages": [],
            "parentMessageId": "client-created-root",
            "safety": {"dismissedInterventionIds": []},
            "userMessageCount": 0,
        }
        self._conversation_id = ""
        self._history: list[dict] = []
        self._parser: Optional[DpuParser] = None

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    async def open(self) -> "MwebChatClient":
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept-Language": "en-US,en;q=0.9",
                    "Origin": BASE_URL,
                    "Referer": f"{BASE_URL}/",
                }
            )
            self._own_session = True
        try:
            async with self._session.get(
                f"{BASE_URL}/", timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                await resp.read()
        except aiohttp.ClientError:
            pass
        await self._ensure_mweb_cookies()
        return self

    async def _ensure_mweb_cookies(self) -> None:
        jar = self._session.cookie_jar
        have = set(jar.filter_cookies(f"{BASE_URL}/"))
        extra = {}
        if "oai-mweb-route" not in have:
            extra["oai-mweb-route"] = "1"
        if "oai-mweb-origin" not in have:
            extra["oai-mweb-origin"] = "1"
        if "oai-did" not in have:
            extra["oai-did"] = str(uuid.uuid4())
        if extra:
            jar.update_cookies(extra, URL(f"{BASE_URL}/"))

    async def close(self) -> None:
        if self._own_session and self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def __aenter__(self):
        return await self.open()

    async def __aexit__(self, *args):
        await self.close()

    @property
    def conversation_id(self) -> str:
        return self._conversation_id

    @property
    def history(self) -> list[dict]:
        return list(self._history)

    @property
    def conversation_state(self) -> dict:
        return dict(self._conv_state)

    def new_conversation(self) -> None:
        self._conv_state = {
            "messages": [],
            "parentMessageId": "client-created-root",
            "safety": {"dismissedInterventionIds": []},
            "userMessageCount": 0,
        }
        self._conversation_id = ""
        self._history = []

    # ------------------------------------------------------------------
    # transport helpers
    # ------------------------------------------------------------------
    async def _require_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            await self.open()
        return self._session

    def _headers(self, referer: str, accept: str = "*/*", ct: str = "") -> dict:
        headers = {
            "Accept": accept,
            "Referer": referer,
            "oai-session-id": self._session_id,
        }
        if ct:
            headers["Content-Type"] = ct
        return headers

    async def _post_json(self, url: str, payload: dict, referer: str, timeout: float = 60) -> dict:
        session = await self._require_session()
        async with session.post(
            url,
            data=json.dumps(payload, separators=(",", ":")),
            headers=self._headers(referer, "application/json", JSON_CT),
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            text = await resp.text()
            if resp.status != 200:
                if resp.status in (401, 403):
                    raise AuthError(f"{url.rsplit('/', 2)[-2]} failed {resp.status}: {text[:300]}")
                raise UpstreamError(
                    f"{url.rsplit('/', 2)[-2]} failed {resp.status}: {text[:400]}",
                    status=resp.status,
                    body=text,
                )
            try:
                return json.loads(text)
            except json.JSONDecodeError as e:
                raise UpstreamError(f"{url} returned non-JSON: {text[:300]}") from e

    # ------------------------------------------------------------------
    # steps
    # ------------------------------------------------------------------
    async def _sentinel(self) -> Tuple[str, str, str]:
        """
        chat-requirements over plain HTTP.

        Returns (token, prepare_token, proof_token) - all three land in the
        updates form body.
        """
        referer = f"{BASE_URL}/"
        prepare = await self._post_json(
            f"{SENTINEL_URL}/prepare", {"p": build_requirements_blob(self._profile)}, referer
        )

        body: dict = {"prepare_token": prepare.get("prepare_token", "")}
        proof = ""
        pow_spec = prepare.get("proofofwork") or {}
        if pow_spec.get("required") and pow_spec.get("seed") and pow_spec.get("difficulty"):
            answer = await asyncio.to_thread(
                solve_pow,
                pow_spec["seed"],
                str(pow_spec["difficulty"]),
                self._profile,
                MAX_POW_ATTEMPTS,
                time.time() + 30,
            )
            if not answer:
                raise UpstreamError("proof-of-work did not converge before the deadline")
            proof = PROOF_PREFIX + answer
            body["proofofwork"] = proof

        turnstile = prepare.get("turnstile") or {}
        if turnstile.get("required") and not turnstile.get("dx"):
            raise TurnstileRequiredError(
                "chat-requirements requested a Turnstile challenge; "
                "use `python -m gptpp.client --browser` (Playwright) instead"
            )

        final = await self._post_json(f"{SENTINEL_URL}/finalize", body, referer)
        token = final.get("token") or ""
        if not token:
            raise UpstreamError("chat-requirements finalize returned no token")
        return token, str(prepare.get("prepare_token") or ""), proof

    async def _prepare(self, referer: str) -> str:
        session = await self._require_session()
        form = self._form_fields()
        form["conversationRetryOwner"] = json.dumps(
            {"mode": "anonymous", "sessionEpoch": None}, separators=(",", ":")
        )
        headers = self._headers(referer, "*/*", FORM_CT)
        headers.update(
            {
                "x-oai-turn-trace-id": str(uuid.uuid4()),
                "x-web-mobile-prepare-state": "none",
                "x-web-mobile-prepare-reason": "initial",
            }
        )
        async with session.post(
            f"{PREPARE_URL}?lightweight_authenticated=0",
            data=form,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=60),
        ) as resp:
            text = await resp.text()
            if resp.status != 200:
                if resp.status in (401, 403):
                    raise AuthError(f"conversation prepare failed {resp.status}: {text[:300]}")
                raise UpstreamError(
                    f"conversation prepare failed {resp.status}: {text[:400]}",
                    status=resp.status,
                    body=text,
                )
            try:
                payload = json.loads(text)
            except json.JSONDecodeError as e:
                raise UpstreamError(f"conversation prepare returned non-JSON: {text[:300]}") from e
        conduit = str(payload.get("conduit_token") or "")
        if not conduit:
            raise UpstreamError(f"conversation prepare returned no conduit token: {text[:300]}")
        return conduit

    def _form_fields(self) -> dict:
        return {
            "conversationState": json.dumps(self._conv_state, separators=(",", ":")),
            "clientContextualInfo": _client_context(),
            "timezone": time.tzname[0],
            "timezoneOffsetMinutes": str(_offset_minutes()),
            "oai-session-id": self._session_id,
        }

    # ------------------------------------------------------------------
    # streaming
    # ------------------------------------------------------------------
    async def _stream_prompt(
        self, prompt: str, timeout: float = 180
    ) -> AsyncGenerator[dict, None]:
        session = await self._require_session()
        referer = (
            f"{BASE_URL}/uc/{self._conversation_id}"
            if self._conversation_id
            else f"{BASE_URL}/"
        )

        requirements, prepare_token, proof = await self._sentinel()
        conduit = await self._prepare(referer)

        form = self._form_fields()
        form.update(
            {
                "messageMetadata": "{}",
                "imageAttachments": "[]",
                "pendingImageUploads": "[]",
                "prompt": prompt,
                "chatRequirementsToken": requirements,
                "chatRequirementsPrepareToken": prepare_token,
                "proofToken": proof,
                "turnstileToken": "",
                "telemetryToken": "",
                "timingToken": "[1,null]",
                "imageSaveData": "unknown",
                "imageEffectiveType": "unknown",
                "sessionObserverToken": "",
                "workOrigin": "",
                "workModel": "",
                "workEffort": "",
                "workTier": "",
                "assistantMessageId": f"pending-{uuid.uuid4()}",
                "userMessageId": str(uuid.uuid4()),
            }
        )
        headers = self._headers(referer, DPU_CONTENT_TYPE, FORM_CT)
        headers.update(
            {
                "x-conduit-token": conduit,
                "x-oai-turn-trace-id": str(uuid.uuid4()),
                "x-web-mobile-prepare-state": "success",
                "x-web-mobile-conversation-renderer": "octane",
                "x-web-mobile-conversation-stream-protocol": "2",
            }
        )
        url = f"{UPDATES_URL}?lightweight_authenticated=0&operationId={uuid.uuid4()}"

        parser = DpuParser()
        self._parser = parser
        decoder = codecs.getincrementaldecoder("utf-8")(errors="ignore")
        stream_error: Optional[str] = None

        async with session.post(
            url,
            data=form,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                if resp.status in (401, 403):
                    raise AuthError(f"conversation updates rejected ({resp.status}): {body[:300]}")
                raise UpstreamError(
                    f"conversation updates failed {resp.status}: {body[:400]}",
                    status=resp.status,
                    body=body,
                )
            finished = False
            async for raw in resp.content.iter_any():
                if not raw:
                    continue
                for event in parser.feed(decoder.decode(raw)):
                    if event["kind"] == "error":
                        stream_error = event["message"]
                    yield event
                    if event["kind"] in ("done", "error"):
                        finished = True
                        break
                if finished:
                    break

        if stream_error or parser.failure:
            return
        if parser.done or parser.terminated:
            yield {"kind": "done"}
        else:
            yield {
                "kind": "error",
                "message": "conversation stream ended before completion",
                "failure": {},
            }

    def _adopt_turn_state(self) -> None:
        parser = self._parser
        if parser is None:
            return
        if parser.conversation_state:
            self._conv_state = parser.conversation_state
        elif parser.terminated:
            self._conv_state = {**self._conv_state, "userMessageCount": int(
                self._conv_state.get("userMessageCount") or 0) + 1}
        if parser.conversation_id:
            self._conversation_id = parser.conversation_id

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------
    async def send_message_stream(
        self, message: str, timeout: float = 180
    ) -> AsyncGenerator[str, None]:
        """Yield answer deltas for `message`, keeping the conversation alive."""
        self._parser = None
        self._history.append({"role": "user", "content": message})
        answer = ""
        error: Optional[str] = None
        try:
            async for event in self._stream_prompt(message, timeout):
                if event["kind"] == "delta":
                    answer += event["delta"]
                    yield event["delta"]
                elif event["kind"] == "error":
                    error = event["message"]
        except (AuthError, UpstreamError, TurnstileRequiredError):
            raise
        # the parser's own view of the message is authoritative (block
        # re-splits can make the raw delta stream drift by a space/newline)
        final = self._parser.answer if self._parser is not None and self._parser.answer else answer
        if final:
            self._history.append({"role": "assistant", "content": final})
            self._adopt_turn_state()
        elif error:
            raise UpstreamError(error)

    async def send_message(self, message: str, timeout: float = 180) -> str:
        chunks = [c async for c in self.send_message_stream(message, timeout)]
        if self._parser is not None and self._parser.answer:
            return self._parser.answer
        return "".join(chunks)

    async def send_messages_stream(
        self, messages: list, timeout: float = 180
    ) -> AsyncGenerator[str, None]:
        """Replay a full OpenAI-style message list as one flattened prompt."""
        async for delta in self.send_message_stream(flatten_messages(messages), timeout):
            yield delta

    async def send_messages(self, messages: list, timeout: float = 180) -> str:
        return "".join([c async for c in self.send_messages_stream(messages, timeout)])

    async def send_message_full(self, message: str, timeout: float = 180) -> dict:
        """Buffered call - the mobile-web flow has no separate reasoning stream."""
        reply = await self.send_message(message, timeout)
        return {"thinking": "", "response": reply, "conversation_id": self._conversation_id}
