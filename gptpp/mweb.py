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
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple

import aiohttp
from yarl import URL

from .errors import AuthError, TurnstileRequiredError, UpstreamError
from .routing import base as relay_base, cookie_jar, via
from .sentinel import (
    BASE_URL,
    MAX_POW_ATTEMPTS,
    PROOF_PREFIX,
    USER_AGENT,
    BrowserProfile,
    build_requirements_blob,
    solve_pow,
)
from .solver import solve_turnstile

MW_URL = f"{BASE_URL}/unauth-mweb"
UPDATES_URL = f"{MW_URL}/conversation/updates"
PREPARE_URL = f"{MW_URL}/conversation/prepare"
SENTINEL_URL = f"{MW_URL}/sentinel/chat-requirements"

DPU_CONTENT_TYPE = "text/vnd.openai.web-mobile-partial+html"
SOURCE_NDJSON = "application/vnd.openai.conversation-source+ndjson"
FORM_CT = "application/x-www-form-urlencoded;charset=UTF-8"
JSON_CT = "application/json;charset=UTF-8"

_AFFINITY_RE = re.compile(r'data-conversation-document-affinity="([^"]{1,2048})"')
_WORKER_VERSION_RE = re.compile(r'data-worker-version-id="([A-Za-z0-9._-]{1,64})"')
_SESSION_ID_RE = re.compile(
    r'<input[^>]*name="oai-session-id"[^>]*value="([^"]{1,64})"'
)
_SESSION_ID_JSON_RE = re.compile(r'"sessionId":"([0-9a-fA-F-]{36})"')

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
_SOURCES_PAYLOAD_RE = re.compile(r'data-assistant-sources-payload="([^"]*)"')
_SHIMMER_RE = re.compile(r'data-text-shimmer-text="([^"]*)"')

# blocks that carry prose; interactive blocks (button footnotes, chips) do not
_PROSE_TAGS = frozenset(
    ("p", "li", "h1", "h2", "h3", "h4", "h5", "h6",
     "blockquote", "pre", "td", "th", "figcaption", "dd", "dt")
)
_VOID_TAGS = frozenset(
    ("img", "br", "hr", "input", "meta", "link", "source", "track", "wbr", "col", "area", "base", "embed")
)
# token scanner tolerant of `>` inside quoted attribute values (payload JSON)
_TOKEN_RE = re.compile(r"<(/?)([a-zA-Z][\w-]*)((?:[^>\"']|\"[^\"]*\"|'[^']*')*)>", re.S)


def _is_tool_node(tag: str, attrs: str) -> bool:
    """Citation chips, grouped-webpage buttons, source footnotes."""
    if ("data-assistant-entity-reference" in attrs
            or 'data-content-reference-type="entity"' in attrs):
        # inline entity chip: its name IS the prose word (`NASA's
        # [Curiosity rover] has captured ...`) - unwrap, never drop
        return False
    return (
        "data-assistant-content-reference" in attrs
        or "data-assistant-grouped-webpages" in attrs
        or "data-assistant-sources-trigger" in attrs
        or "data-assistant-sources-payload" in attrs
        or tag == "button"
    )


def drop_tool_nodes(fragment: str) -> str:
    """
    Remove tool/citation UI subtrees (buttons and their wrapper spans) so
    only prose text survives.
    """
    out: List[str] = []
    skip: List[str] = []          # tag names of subtrees being skipped
    pos = 0
    for m in _TOKEN_RE.finditer(fragment):
        if not skip:
            out.append(fragment[pos:m.start()])
        pos = m.end()
        closing, tag, attrs = m.group(1), m.group(2).lower(), m.group(3)
        self_close = m.group(0).rstrip().endswith("/>") or tag in _VOID_TAGS
        if skip:
            if closing:
                if tag == skip[-1]:
                    skip.pop()
            elif not self_close:
                skip.append(tag)
        elif not closing and not self_close and _is_tool_node(tag, attrs):
            skip.append(tag)
    if not skip:
        out.append(fragment[pos:])
    return "".join(out)



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


SYSTEM_PREFIX = "System instruction:"


def apply_system_prompt(text: str, system_prompt: str) -> str:
    """
    Prepend standing instructions to a prompt.

    chatgpt.com has no system role - both transports deliver instructions
    in-band as a `System instruction:` header. Idempotent: text that already
    carries the header is returned untouched.
    """
    prompt = (system_prompt or "").strip()
    if not prompt:
        return text
    if text.lstrip().startswith(SYSTEM_PREFIX):
        return text
    return f"{SYSTEM_PREFIX} {prompt}\n\n{text}"


def flatten_messages(messages: list, system_prompt: str = "") -> str:
    """
    Transcript-style prompt for a brand-new conversation.

    `system`/`developer` turns become the `System instruction:` header; when
    the list carries none, the caller's `system_prompt` fills that slot
    (explicit system messages always win over the client default).
    """
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
    header = "\n".join(system_bits) or (system_prompt or "").strip()
    if header:
        out = f"{SYSTEM_PREFIX} {header}\n\n"
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


_CITATION_RUN_RE = re.compile(
    r"[ \t]*\burl[^<>]*?turn\d+search\d+(?:[ \t]+url[^<>]*?turn\d+search\d+)*(?:[ \t]+url)?"
)
# dangling footer heading left behind after its chips were stripped as UI
_SOURCES_HEADING_RE = re.compile(r"(?<![^.\s])Sources\s*:?\s*$")


def _tidy_prose(text: str) -> str:
    """Drop citation runs and dangling sources-chrome from prose."""
    text = _CITATION_RUN_RE.sub("", text)
    return _SOURCES_HEADING_RE.sub("", text).rstrip()


def extract_assistant_text(content: str) -> str:
    """
    Pull assistant *prose* out of one DPU frame.

    Live/committed blocks arrive as `<tag data-assistant-stream-block-index="N">`
    elements (paragraphs, lists, headings); older frames carry a
    `data-writing-block-source` JSON attribute instead. Tool UI (citation
    chips, grouped-webpage buttons, source footnotes) and interactive blocks
    are stripped so only the answer text survives.
    """
    if not content:
        return ""
    blocks = _STREAM_BLOCK_RE.findall(content)
    if blocks:
        blocks.sort(key=lambda pair: int(pair[1]))
        parts = []
        for tag, _, inner in blocks:
            if tag.lower() not in _PROSE_TAGS:
                continue
            text = _tidy_prose(strip_markup(drop_tool_nodes(inner)))
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
        return _tidy_prose("".join(text for _, text in sources))

    return _tidy_prose(strip_markup(drop_tool_nodes(content)))


def harvest_sources(content: str) -> List[dict]:
    """All `data-assistant-sources-payload` JSON blobs in one frame."""
    out: List[dict] = []
    seen: set = set()
    for m in _SOURCES_PAYLOAD_RE.finditer(content):
        try:
            payload = json.loads(html.unescape(m.group(1)))
        except (json.JSONDecodeError, ValueError, TypeError):
            continue
        if not isinstance(payload, list):
            continue
        for item in payload:
            if not isinstance(item, dict):
                continue
            key = (str(item.get("url") or ""), str(item.get("title") or ""))
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "title": str(item.get("title") or ""),
                "url": str(item.get("url") or ""),
                "attribution": str(item.get("attribution") or ""),
            })
    return out


_SHIMMER_TEXT_RE = re.compile(r'data-text-shimmer-text="[^"]*">([^<]+)</span>')


def harvest_status(content: str) -> List[str]:
    """Tool status lines (`Searching the web`, `Searched 18 websites`, ...)."""
    out: List[str] = []
    for pattern in (_SHIMMER_TEXT_RE, _SHIMMER_RE):
        for m in pattern.finditer(content):
            text = html.unescape(m.group(1)).strip()
            if text and text not in out:
                out.append(text)
    return out


_ASSISTANT_SUFFIX_RE = re.compile(r"-(?:pending(?:-tail)?|committed(?:-[\w-]+)?)$")


def assistant_group(name: str) -> str:
    """
    `assistant-pending-<uuid>-committed-tail` -> `assistant-pending-<uuid>`
    (same for `-pending` / `-pending-tail`).

    The pending (streaming) region, its append-only tail and the committed
    region of one message share a group id; whatever the server has
    committed wins over what is still pending, and pending text is
    assembled as head (`-pending`, replace) + tail (`-pending-tail`,
    append).
    """
    return _ASSISTANT_SUFFIX_RE.sub("", name)


def _walk_emitted(emitted: str, full: str) -> "tuple[str, str]":
    """Prefix-walk old vs new answer text; return (delta, new_emitted).

    Never retracts what was already streamed: a shrink that keeps the
    emitted prefix stays put, whitespace-only drift is skipped, and only
    genuinely new text is returned as the delta.
    """
    if full == emitted:
        return "", emitted
    if len(full) < len(emitted) and emitted.startswith(full):
        return "", emitted
    i = j = 0
    while i < len(emitted) and j < len(full):
        old_c, new_c = emitted[i], full[j]
        if old_c == new_c:
            i += 1
            j += 1
            continue
        if old_c.isspace() and new_c.isspace():
            while i < len(emitted) and emitted[i].isspace():
                i += 1
            while j < len(full) and full[j].isspace():
                j += 1
            continue
        if new_c.isspace():
            j += 1     # server inserted whitespace (paragraph split)
            continue
        if old_c.isspace():
            i += 1     # server dropped whitespace
            continue
        break
    return full[j:], full


def adopt_state(holder, raw: str) -> None:
    if not raw:
        return
    try:
        state = json.loads(html.unescape(raw))
    except (json.JSONDecodeError, ValueError):
        return
    if isinstance(state, dict) and state:
        holder.conversation_state = state


def apply_controls(holder, content: str) -> List[dict]:
    """Recognize `data-conversation-control` spans; update `holder` flags."""
    events: List[dict] = []
    matches = list(_CONTROL_RE.finditer(content))
    if not matches:
        matches = list(_DOC_CONTROL_RE.finditer(content))
    for match in matches:
        name = match.group(1)
        attrs = dict(_ATTR_RE.findall(match.group(2)))
        holder.controls.append(name)
        if name == "conversation-id":
            if attrs.get("conversation-id"):
                holder.conversation_id = attrs["conversation-id"]
        elif name == "message-stream-complete":
            if attrs.get("conversation-id"):
                holder.conversation_id = attrs["conversation-id"]
            holder.terminated = True
            if attrs.get("message-id"):
                holder.message_id = attrs["message-id"]
            adopt_state(holder, attrs.get("conversation-state"))
        elif name in ("terminal-received", "complete"):
            holder.terminated = True
            if attrs.get("message-id"):
                holder.message_id = attrs["message-id"]
            adopt_state(holder, attrs.get("conversation-state"))
            if name == "complete":
                holder.done = True
        elif name == "failed":
            holder.failure = {
                "reason": attrs.get("failure-reason") or "",
                "status": attrs.get("failure-status") or "",
                "origin": attrs.get("failure-origin") or "",
            }
        events.append({"kind": "control", "name": name, "attrs": attrs})
    return events


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

    TRACE = False   # test harness flips this on for composition postmortems

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
        self.status: List[str] = []      # tool activity, in order
        self.sources: List[dict] = []     # citation payloads seen this turn
        self.trace: list = []             # composition trace when TRACE is on

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
                if apply == "replace" and name.endswith("-pending"):
                    self.pending.pop(name + "-tail", None)
                for line in harvest_status(content):
                    if line not in self.status:
                        self.status.append(line)
                        events.append({"kind": "status", "text": line})
                found = harvest_sources(content)
                if found and found != self.sources:
                    self.sources = found
                    events.append({"kind": "sources", "sources": [dict(s) for s in found]})
                # `-reasoning` templates only carry tool status / chain-of-
                # thought chrome - never answer prose
                if not name.endswith("-reasoning"):
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
        return apply_controls(self, content)

    def _adopt_state(self, raw: str) -> None:
        adopt_state(self, raw)

    # ------------------------------------------------------------------
    def _apply_text(self, name: str, apply: str, text: str) -> list[dict]:
        emitted_before = self.emitted
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
        events: list[dict] = []
        if full == self.emitted:
            pass
        # a pending -> committed swap can shrink or reorder the assembled
        # text; never retract what was already streamed and never re-emit
        # it (regrowth then continues from the longer prefix, no duplicates)
        elif len(full) < len(self.emitted) and self.emitted.startswith(full):
            pass
        else:
            # whitespace-tolerant prefix walk: the server may re-split a
            # boundary (`2028.  ` vs `2028.\n `) - never re-emit across ws
            delta, self.emitted = _walk_emitted(self.emitted, full)
            if delta:
                events = [{"kind": "delta", "delta": delta}]
        if self.TRACE:
            self.trace.append({
                "name": name, "apply": apply, "text": text,
                "answer": full, "emitted_before": emitted_before,
                "emitted_after": self.emitted,
                "delta": "".join(e["delta"] for e in events),
                "pending": dict(self.pending),
                "committed": dict(self.committed),
            })
        return events

    # ------------------------------------------------------------------
    @property
    def answer(self) -> str:
        """Final text: committed region wins over the still-pending one."""
        out: List[str] = []
        for group in self.group_order:
            keys = self.group_keys[group]
            committed_keys = [k for k in keys if k in self.committed]
            if committed_keys:
                # long answers commit incrementally: `committed-tail` appends
                # each finalized chunk while `committed-block-N` directives
                # re-send the same content per block - the tail alone is the
                # complete message, so blocks only serve as fallback
                tail_keys = [k for k in committed_keys
                             if k.endswith("-committed-tail")]
                if tail_keys:
                    out.extend(self.committed[k] for k in tail_keys)
                else:
                    def block_no(key: str) -> int:
                        suffix = key.rsplit("-", 1)[-1]
                        return int(suffix) if suffix.isdigit() else -1
                    block_keys = sorted(
                        (k for k in committed_keys if block_no(k) >= 0),
                        key=block_no,
                    )
                    out.extend(self.committed[k] for k in block_keys)
                    out.extend(self.committed[k] for k in committed_keys
                               if k not in block_keys)
                continue
            pending_keys = [k for k in keys if k in self.pending]
            pending_keys.sort(key=lambda k: k.endswith("-pending-tail"))
            head = "".join(
                self.pending[k] for k in pending_keys
                if not k.endswith("-pending-tail")
            )
            tail = "".join(
                self.pending[k] for k in pending_keys
                if k.endswith("-pending-tail")
            )
            if tail and head and tail in head:
                tail = ""   # stale tail already covered by the new head
            out.append(head + tail)
        return "".join(out)


class SourceNdjsonParser:
    """
    Incremental parser for `application/vnd.openai.conversation-source+ndjson`.

    Since the document-affinity rollout `conversation/updates` answers in
    newline-delimited JSON instead of DPU frames; feed() keeps DpuParser's
    event contract so the stream loop stays format-agnostic:

        {"kind": "delta",   "delta": str}
        {"kind": "control", "name": str, "attrs": dict}
        {"kind": "status",  "text": str}
        {"kind": "sources", "sources": list}
        {"kind": "done"}
        {"kind": "error",   "message": str, "failure": dict}
    """

    def __init__(self) -> None:
        self.buffer = ""
        self.emitted = ""
        self.conversation_id = ""
        self.conversation_state: dict = {}
        self.message_id = ""
        self.done = False          # control "complete" seen
        self.terminated = False    # "end" event / terminal control seen
        self.failure: Optional[dict] = None
        self.controls: List[str] = []
        self.status: List[str] = []
        self.sources: List[dict] = []
        self.trace: list = []
        self._texts: Dict[str, str] = {}

    @property
    def answer(self) -> str:
        if not self._texts:
            return ""
        last = next(reversed(self._texts))
        return self._texts[last]

    def feed(self, data: str) -> list[dict]:
        self.buffer += data
        events: list[dict] = []
        while True:
            nl = self.buffer.find("\n")
            if nl < 0:
                break
            line = self.buffer[:nl]
            self.buffer = self.buffer[nl + 1:]
            events.extend(self._line(line.strip()))
        return events

    # ------------------------------------------------------------------
    def _line(self, line: str) -> list[dict]:
        if not line:
            return []
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return []
        if not isinstance(event, dict):
            return []
        kind = event.get("type")
        if kind == "content":
            return self._content(event)
        if kind == "shared":
            return self._shared(event.get("html") or "")
        if kind == "control":
            return self._control(event)
        if kind == "end":
            self.terminated = True
            return [{"kind": "done"}]
        if kind in ("error", "diagnostic"):
            detail = event.get("detail") or {}
            message = (
                event.get("message")
                or detail.get("kind")
                or event.get("detailText")
                or "stream error"
            )
            if kind == "diagnostic":
                return [{"kind": "control", "name": str(message), "attrs": {}}]
            self.failure = {"reason": str(message), "status": ""}
            return [{
                "kind": "error",
                "message": f"conversation update failed: {message}",
                "failure": dict(self.failure),
            }]
        return []

    def _content(self, event: dict) -> list[dict]:
        message_id = str(event.get("messageId") or "")
        markdown = event.get("markdown")
        if not isinstance(markdown, str):
            markdown = ""
        if message_id and message_id not in self._texts:
            self._texts[message_id] = ""
        if event.get("mode") == "append" and message_id:
            self._texts[message_id] = self._texts[message_id] + markdown
        elif message_id:
            self._texts[message_id] = markdown
        if message_id:
            self.message_id = message_id
        full = self.answer
        delta, self.emitted = _walk_emitted(self.emitted, full)
        if delta:
            return [{"kind": "delta", "delta": delta}]
        return []

    def _shared(self, html: str) -> list[dict]:
        events: list[dict] = []
        for event in apply_controls(self, html):
            events.append(event)
        for line in harvest_status(html):
            if line not in self.status:
                self.status.append(line)
                events.append({"kind": "status", "text": line})
        found = harvest_sources(html)
        if found and found != self.sources:
            self.sources = found
            events.append({"kind": "sources", "sources": [dict(s) for s in found]})
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

    def _control(self, event: dict) -> list[dict]:
        detail = event.get("detail") or {}
        name = str(detail.get("kind") or "control")
        if name == "conversation-id":
            if detail.get("conversationId"):
                self.conversation_id = str(detail["conversationId"])
        elif name == "failed":
            self.failure = {
                "reason": str(
                    detail.get("failureReason") or detail.get("reason") or ""
                ),
                "status": str(detail.get("status") or ""),
                "origin": str(detail.get("origin") or ""),
            }
            return [{
                "kind": "error",
                "message": (
                    "conversation update failed: "
                    f"{self.failure.get('reason') or 'unknown'}"
                    f" (status {self.failure.get('status') or '?'})"
                ),
                "failure": dict(self.failure),
            }]
        elif name in ("terminal-received", "complete", "message-stream-complete"):
            self.terminated = True
            if name in ("terminal-received", "complete"):
                self.done = self.done or name == "complete"
        self.controls.append(name)
        return [{"kind": "control", "name": name, "attrs": dict(detail)}]


class MwebChatClient:
    """Pure-HTTP chatgpt.com client built on `/unauth-mweb/conversation/updates`."""

    def __init__(
        self,
        session: Optional[aiohttp.ClientSession] = None,
        profile: Optional[BrowserProfile] = None,
        system_prompt: str = "",
    ):
        self._session = session
        self._own_session = session is None
        self._profile = profile or BrowserProfile()
        self.system_prompt = system_prompt
        self._system_state = ""    # instructions already delivered this conversation
        self._system_applied = ""  # what the text built for the current turn carries
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
        self._doc_affinity = ""
        self._worker_version_id = ""
        self.last_turn: dict = {"status": [], "sources": [], "answer": ""}

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
                },
                cookie_jar=cookie_jar(),
            )
            self._own_session = True
        await self._refresh_document()
        await self._ensure_mweb_cookies()
        return self

    async def _refresh_document(self) -> None:
        """Fetch the page HTML and extract this session's document tokens.

        `conversation/updates` requires the per-page
        `X-Web-Mobile-Conversation-Document-Affinity` (plus the matching
        worker version); without it the server answers 409 with
        `X-Web-Mobile-Conversation-Document-Upgrade: required`, and a
        token from another session gets 403 `Invalid conversation
        document affinity`. Both values live in the page's data
        attributes, so the HTML must be fetched through this session.
        """
        if self._session is None or self._session.closed:
            return
        try:
            async with self._session.get(
                via(f"{BASE_URL}/"),
                headers={
                    "Accept": (
                        "text/html,application/xhtml+xml,"
                        "application/xml;q=0.9,*/*;q=0.8"
                    ),
                    "Sec-Fetch-Dest": "document",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Site": "none",
                    "Upgrade-Insecure-Requests": "1",
                },
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    return
                text = await resp.text()
        except aiohttp.ClientError:
            return
        m = _AFFINITY_RE.search(text)
        if m:
            self._doc_affinity = m.group(1)
        m = _WORKER_VERSION_RE.search(text)
        if m:
            self._worker_version_id = m.group(1)
        m = _SESSION_ID_RE.search(text) or _SESSION_ID_JSON_RE.search(text)
        if m:
            # The affinity token is signed over the page's session id, so
            # every request must present the same oai-session-id.
            self._session_id = m.group(1)

    def _document_headers(self) -> dict:
        out = {}
        if self._doc_affinity:
            out["x-web-mobile-conversation-document-affinity"] = (
                self._doc_affinity
            )
        if self._worker_version_id:
            out["x-web-mobile-document-worker-version"] = self._worker_version_id
        return out

    async def _ensure_mweb_cookies(self) -> None:
        jar = self._session.cookie_jar
        have = set(jar.filter_cookies(f"{relay_base()}/"))
        extra = {}
        if "oai-mweb-route" not in have:
            extra["oai-mweb-route"] = "1"
        if "oai-mweb-origin" not in have:
            extra["oai-mweb-origin"] = "1"
        if "oai-did" not in have:
            extra["oai-did"] = str(uuid.uuid4())
        if extra:
            jar.update_cookies(extra, URL(f"{relay_base()}/"))

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
        self._system_state = ""
        self._system_applied = ""

    # ------------------------------------------------------------------
    # system prompt
    # ------------------------------------------------------------------
    def resolve_system(self, system: Optional[str] = None) -> str:
        """Per-call override wins over the client default; `""` disables."""
        raw = self.system_prompt if system is None else system
        return (raw or "").strip()

    def _prompt_with_system(self, message: str, system: str) -> str:
        """
        Attach standing instructions to the outgoing prompt.

        Sent on the conversation's first turn and again whenever the
        instructions change - the server keeps earlier turns in context, so
        repeating them every turn would be noise. `send_message_stream`
        commits `_system_applied` to `_system_state` once the turn lands.
        """
        self._system_applied = self._system_state
        if not system:
            return message
        fresh = not int(self._conv_state.get("userMessageCount") or 0)
        if fresh or self._system_state != system:
            message = apply_system_prompt(message, system)
            self._system_applied = system
        return message

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
            via(url),
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
    async def _post_json_retry(
        self, url: str, payload: dict, referer: str, attempts: int = 3
    ) -> dict:
        """`_post_json`, retrying transient upstream statuses with backoff."""
        for attempt in range(attempts):
            try:
                return await self._post_json(url, payload, referer)
            except UpstreamError as e:
                if e.status in (429, 502, 503, 504) and attempt + 1 < attempts:
                    await asyncio.sleep(3 * (attempt + 1))
                    continue
                raise
        raise AssertionError("unreachable")

    async def _sentinel(self) -> Tuple[str, str, str, str]:
        """
        chat-requirements over plain HTTP.

        Returns (token, prepare_token, proof_token, turnstile_token) - all
        four land in the updates form body (turnstile also in finalize).
        """
        referer = f"{BASE_URL}/"
        p = build_requirements_blob(self._profile)
        prepare = await self._post_json_retry(f"{SENTINEL_URL}/prepare", {"p": p}, referer)

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
        turnstile_token = ""
        if turnstile.get("required"):
            dx = turnstile.get("dx")
            if not dx:
                raise TurnstileRequiredError(
                    "chat-requirements requested a Turnstile challenge without a program"
                )
            turnstile_token = await solve_turnstile(p, dx)
            body["turnstile"] = turnstile_token

        final = await self._post_json_retry(f"{SENTINEL_URL}/finalize", body, referer)
        token = final.get("token") or ""
        if not token:
            raise UpstreamError("chat-requirements finalize returned no token")
        return token, str(prepare.get("prepare_token") or ""), proof, turnstile_token

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
            via(f"{PREPARE_URL}?lightweight_authenticated=0"),
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
        self, prompt: str, timeout: float = 180, _attempt: int = 0
    ) -> AsyncGenerator[dict, None]:
        session = await self._require_session()
        referer = (
            f"{BASE_URL}/uc/{self._conversation_id}"
            if self._conversation_id
            else f"{BASE_URL}/"
        )

        requirements, prepare_token, proof, turnstile_token = await self._sentinel()
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
                "turnstileToken": turnstile_token,
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
        headers = self._headers(referer, SOURCE_NDJSON, FORM_CT)
        headers.update(
            {
                "x-conduit-token": conduit,
                "x-oai-turn-trace-id": str(uuid.uuid4()),
                "x-web-mobile-prepare-state": "success",
                "x-web-mobile-conversation-source": "1",
                "x-web-mobile-conversation-renderer": "octane",
                "x-web-mobile-conversation-stream-protocol": "2",
            }
        )
        headers.update(self._document_headers())
        url = f"{UPDATES_URL}?lightweight_authenticated=0&operationId={uuid.uuid4()}"

        parser: Any = None
        self._parser = None
        decoder = codecs.getincrementaldecoder("utf-8")(errors="ignore")
        stream_error: Optional[str] = None

        async with session.post(
            via(url),
            data=form,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                stale_document = resp.status == 409 or (
                    resp.status == 403 and "affinity" in body.lower()
                )
                if stale_document and _attempt == 0:
                    await self._refresh_document()
                    async for event in self._stream_prompt(
                        prompt, timeout, _attempt=1
                    ):
                        yield event
                    return
                if resp.status in (401, 403):
                    raise AuthError(f"conversation updates rejected ({resp.status}): {body[:300]}")
                raise UpstreamError(
                    f"conversation updates failed {resp.status}: {body[:400]}",
                    status=resp.status,
                    body=body,
                )
            # document-affinity era: ndjson above the wire; older code
            # paths still speak DPU frames - pick by the answer's type
            ctype = resp.headers.get("content-type", "")
            parser = (
                SourceNdjsonParser() if "ndjson" in ctype else DpuParser()
            )
            self._parser = parser
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

        if parser is None:
            return
        # flush a final line that closed without a trailing newline
        for event in parser.feed("\n"):
            if event["kind"] == "error":
                stream_error = event["message"]
            yield event
            if event["kind"] in ("done", "error"):
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
    async def send_message_events(
        self, message: str, timeout: float = 180, system: Optional[str] = None
    ) -> AsyncGenerator[dict, None]:
        """
        Yield stream events for `message`.

            {"kind": "delta",   "delta": str}     answer prose only
            {"kind": "status",  "text": str}      tool activity (search, ...)
            {"kind": "sources", "sources": list}  citations for this turn
            {"kind": "error",   "message": str}   surfaced as UpstreamError

        Tool status and sources never enter the answer channel; `last_turn`
        records `status`, `sources` and the authoritative final `answer`
        once the turn settles.
        """
        prompt = self._prompt_with_system(message, self.resolve_system(system))
        self._parser = None
        self._history.append({"role": "user", "content": message})
        answer = ""
        error: Optional[str] = None
        try:
            async for event in self._stream_prompt(prompt, timeout):
                if event["kind"] == "delta":
                    answer += event["delta"]
                    yield event
                elif event["kind"] in ("status", "sources"):
                    yield event
                elif event["kind"] == "error":
                    error = event["message"]
        except (AuthError, UpstreamError, TurnstileRequiredError):
            raise
        parser = self._parser
        # the parser's own view of the message is authoritative (block
        # re-splits can make the raw delta stream drift by a space/newline)
        final = parser.answer if parser is not None and parser.answer else answer
        self.last_turn = {
            "status": list(parser.status) if parser is not None else [],
            "sources": [dict(s) for s in parser.sources] if parser is not None else [],
            "answer": final,
        }
        if final:
            self._history.append({"role": "assistant", "content": final})
            self._adopt_turn_state()
            self._system_state = self._system_applied
        elif error:
            raise UpstreamError(error)

    async def send_message_stream(
        self, message: str, timeout: float = 180, system: Optional[str] = None
    ) -> AsyncGenerator[str, None]:
        """Yield clean answer deltas for `message` (tool events filtered out)."""
        async for event in self.send_message_events(message, timeout, system=system):
            if event["kind"] == "delta":
                yield event["delta"]

    async def send_message(
        self, message: str, timeout: float = 180, system: Optional[str] = None
    ) -> str:
        chunks = [
            c async for c in self.send_message_stream(message, timeout, system=system)
        ]
        if self._parser is not None and self._parser.answer:
            return self._parser.answer
        return "".join(chunks)

    async def send_messages_stream(
        self, messages: list, timeout: float = 180, system: Optional[str] = None
    ) -> AsyncGenerator[str, None]:
        """Replay a full OpenAI-style message list as one flattened prompt."""
        effective = self.resolve_system(system)
        prompt = flatten_messages(messages, system_prompt=effective)
        async for delta in self.send_message_stream(prompt, timeout, system=effective):
            yield delta

    async def send_messages(
        self, messages: list, timeout: float = 180, system: Optional[str] = None
    ) -> str:
        chunks = [
            c async for c in self.send_messages_stream(messages, timeout, system=system)
        ]
        if self._parser is not None and self._parser.answer:
            return self._parser.answer
        return "".join(chunks)

    async def send_message_full(
        self, message: str, timeout: float = 180, system: Optional[str] = None
    ) -> dict:
        """Buffered call - the mobile-web flow has no separate reasoning stream."""
        reply = await self.send_message(message, timeout, system=system)
        return {"thinking": "", "response": reply, "conversation_id": self._conversation_id}
