#!/usr/bin/env python3
"""
chatgpt.com API client.

Pure HTTP - after `python -m gptpp.login` (or a token/cookie paste) no browser
is needed: chat-requirements are minted by gptpp.sentinel, completions are
posted to /backend-api/conversation and the SSE stream is parsed here.

When OpenAI's sentinel enforcement demands a Turnstile challenge or a Sentinel
SDK token (HTTP 403 "Unusual activity has been detected…") the request is
replayed through the anonymous `/unauth-mweb/conversation/updates` flow
(gptpp.mweb) - still pure HTTP - and only then, with browser_fallback=True,
through the Playwright client in gptpp/client.py.
"""

from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import os
import time
import uuid
from typing import Any, AsyncGenerator, Optional

import aiohttp

from .errors import AttachmentError, AuthError, SentinelError, UpstreamError
from .mweb import MwebChatClient, apply_system_prompt, flatten_messages
from .sentinel import ChatRequirements, USER_AGENT as SENTINEL_UA, get_chat_requirements

CREDENTIALS_FILE = os.path.join(os.path.dirname(__file__), ".chatgpt_credentials.json")

USER_AGENT = SENTINEL_UA
DEFAULT_MODEL = "auto"
DEFAULT_MODELS = [
    "auto",
    "gpt-5.5",
    "gpt-5.4",
    "gpt-5-5-thinking",
    "gpt-5-5-pro",
    "gpt-5-5-mini",
    "gpt-5-nano",
    "gpt-4o",
]


def _local_tz_name() -> str:
    try:
        import time as _time
        name = _time.tzname[0] if _time.daylight else _time.tzname[0]
        return name or "UTC"
    except Exception:
        return "UTC"


def _timezone_offset_min() -> int:
    # JS Date#getTimezoneOffset(): minutes *behind* UTC
    if time.daylight and time.altzone:
        offset = time.altzone if time.localtime().tm_isdst else time.timezone
    else:
        offset = time.timezone
    return offset // 60


class ChatGptApiClient:
    BASE_URL = "https://chatgpt.com"
    API_URL = "https://chatgpt.com/backend-api"

    def __init__(
        self,
        token: str = "",
        cookie: str = "",
        email: str = "",
        user_id: str = "",
        logged_in: bool = False,
        browser_fallback: bool = False,
        mweb_fallback: bool = True,
        system_prompt: str = "",
        session: Optional[aiohttp.ClientSession] = None,
    ):
        self.token = token
        self.cookie = cookie
        self.email = email
        self.user_id = user_id
        self.logged_in = logged_in
        self.browser_fallback = browser_fallback
        self.mweb_fallback = mweb_fallback
        self.system_prompt = system_prompt
        self._system_state = ""    # instructions already delivered this conversation
        self._system_applied = ""  # what the text built for the current turn carries

        self._session = session
        self._own_session = session is None
        self._requirements: Optional[ChatRequirements] = None
        self._turnstile_token = ""
        self._proof_token = ""
        self._sentinel_token = ""

        self._chat_id: Optional[str] = None
        self._parent_msg_id: Optional[str] = None
        self._history: list[dict] = []
        self._browser = None
        self._mweb: Optional[MwebChatClient] = None
        self._mweb_chat_id: Optional[str] = None
        self._mweb_notice = False
        self._mweb_final: str = ""

    # ------------------------------------------------------------------
    # credentials
    # ------------------------------------------------------------------
    @staticmethod
    def _claims(token: str) -> dict:
        try:
            parts = token.split(".")
            if len(parts) < 2:
                return {}
            padded = parts[1] + "=" * (-len(parts[1]) % 4)
            return json.loads(base64.urlsafe_b64decode(padded))
        except Exception:
            return {}

    @classmethod
    def from_saved_credentials(cls, credentials_path: str = CREDENTIALS_FILE,
                               **kwargs) -> Optional["ChatGptApiClient"]:
        if not os.path.exists(credentials_path):
            return None
        try:
            with open(credentials_path) as f:
                creds = json.load(f)
        except (json.JSONDecodeError, OSError):
            return None
        if not creds.get("token") and not creds.get("cookie"):
            return None
        return cls(
            token=creds.get("token", ""),
            cookie=creds.get("cookie", ""),
            email=creds.get("email", ""),
            user_id=creds.get("user_id", ""),
            logged_in=bool(creds.get("logged_in")),
            **kwargs,
        )

    @classmethod
    def from_env(cls, **kwargs) -> Optional["ChatGptApiClient"]:
        token = os.environ.get("CHATGPT_ACCESS_TOKEN", "")
        cookie = os.environ.get("CHATGPT_COOKIE", "")
        if not token and not cookie:
            return None
        return cls(
            token=token,
            cookie=cookie,
            email=os.environ.get("CHATGPT_EMAIL", ""),
            user_id=os.environ.get("CHATGPT_USER_ID", ""),
            logged_in=bool(token),
            **kwargs,
        )

    @classmethod
    def auto_init(cls, credentials_path: str = CREDENTIALS_FILE, **kwargs) -> Optional["ChatGptApiClient"]:
        return cls.from_env(**kwargs) or cls.from_saved_credentials(credentials_path, **kwargs)

    def save_credentials(self, path: str = CREDENTIALS_FILE):
        existing = {}
        if os.path.exists(path):
            try:
                with open(path) as f:
                    existing = json.load(f)
            except (json.JSONDecodeError, OSError):
                existing = {}
        creds = {
            **existing,
            "token": self.token,
            "cookie": self.cookie,
            "email": self.email,
            "user_id": self.user_id,
            "logged_in": self.logged_in,
            "saved_at": int(time.time()),
        }
        with open(path, "w") as f:
            json.dump(creds, f, indent=2)

    # ------------------------------------------------------------------
    # session / auth
    # ------------------------------------------------------------------
    async def refresh_session(self) -> str:
        """GET /api/auth/session - needs the logged-in session cookie."""
        if not self.cookie:
            raise AuthError("no cookies saved - run: python -m gptpp.login")
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Referer": f"{self.BASE_URL}/",
            "Cookie": self.cookie,
        }
        async with aiohttp.ClientSession(headers=headers) as s:
            async with s.get(f"{self.BASE_URL}/api/auth/session",
                             timeout=aiohttp.ClientTimeout(total=30)) as resp:
                text = await resp.text()
                if resp.status != 200:
                    raise AuthError(f"session fetch failed {resp.status}: {text[:300]}")
                try:
                    data = json.loads(text)
                except json.JSONDecodeError as e:
                    raise AuthError(f"session fetch returned non-JSON: {text[:300]}") from e
        token = data.get("accessToken") or ""
        if not token:
            raise AuthError(
                "chatgpt.com session has no accessToken - log in first: python -m gptpp.login"
            )
        self.token = token
        self.logged_in = True
        user = data.get("user") or {}
        self.email = str(user.get("email") or self.email or "")
        self.user_id = str(user.get("id") or self.user_id or "")
        try:
            self.save_credentials()
        except OSError:
            pass
        try:
            await self.close()
        except Exception:
            pass
        return token

    async def refresh_requirements(self, force: bool = False) -> ChatRequirements:
        """Mint (or reuse) the OpenAI-Sentinel chat-requirements token."""
        if not force and self._requirements and not self._requirements.expired:
            return self._requirements
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Origin": self.BASE_URL,
            "Referer": f"{self.BASE_URL}/",
            "Content-Type": "application/json",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.cookie:
            headers["Cookie"] = self.cookie
        async with aiohttp.ClientSession(headers=headers) as s:
            if not self.cookie:
                # chatgpt.com 401s the sentinel endpoints without first-party
                # cookies - a warm-up GET collects them into the jar
                try:
                    async with s.get(f"{self.BASE_URL}/",
                                     timeout=aiohttp.ClientTimeout(total=30)) as warm:
                        await warm.read()
                except Exception:
                    pass
            requirements = await get_chat_requirements(
                s, turnstile_token=self._turnstile_token
            )
        self._requirements = requirements
        return requirements

    def _headers(self, extra: Optional[dict] = None) -> dict:
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Content-Type": "application/json",
            "Origin": self.BASE_URL,
            "Referer": f"{self.BASE_URL}/",
            "sec-ch-ua": '"Firefox";v="141", "Not?A_Brand";v="99"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Linux"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.cookie:
            headers["Cookie"] = self.cookie
        if extra:
            headers.update(extra)
        return headers

    async def _ensure_session(self):
        loop = asyncio.get_running_loop()
        if self._session is not None and not self._session.closed:
            sess_loop = getattr(self._session, "_loop", None)
            if sess_loop is loop:
                return
            try:
                await self._session.close()
            except Exception:
                pass
            self._session = None
        self._session = aiohttp.ClientSession(headers=self._headers(), loop=loop)
        self._own_session = True
        if not self.cookie:
            # chatgpt.com answers API/sentinel routes with 401 until we hold
            # its first-party cookies - a warm-up GET fills the jar
            try:
                async with self._session.get(
                    f"{self.BASE_URL}/", timeout=aiohttp.ClientTimeout(total=30)
                ) as warm:
                    await warm.read()
            except Exception:
                pass

    async def close(self):
        if self._own_session and self._session and not self._session.closed:
            await self._session.close()
            self._session = None
        if self._browser is not None:
            browser, self._browser = self._browser, None
            try:
                await browser.close()
            except Exception:
                pass
        if self._mweb is not None:
            mweb, self._mweb = self._mweb, None
            try:
                await mweb.close()
            except Exception:
                pass

    async def __aenter__(self):
        await self._ensure_session()
        return self

    async def __aexit__(self, *args):
        await self.close()

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    async def models(self) -> list[str]:
        """GET /backend-api/models (falls back to a static slug list)."""
        try:
            await self._ensure_session()
            async with self._session.get(
                f"{self.API_URL}/models", timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                if resp.status != 200:
                    return list(DEFAULT_MODELS)
                data = await resp.json()
        except Exception:
            return list(DEFAULT_MODELS)
        items = data.get("models") if isinstance(data, dict) else data
        if isinstance(items, dict):
            items = items.get("list") or []
        slugs = []
        for m in items or []:
            if isinstance(m, dict):
                slug = m.get("slug") or m.get("id") or m.get("model")
                if slug:
                    slugs.append(str(slug))
            elif m:
                slugs.append(str(m))
        return slugs or list(DEFAULT_MODELS)

    async def _fetch_leaf(self, chat_id: str) -> Optional[str]:
        """Leaf message id of an existing conversation (best effort)."""
        try:
            await self._ensure_session()
            async with self._session.get(
                f"{self.API_URL}/conversation/{chat_id}",
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        leaf = data.get("current_leaf_node")
        mapping = data.get("mapping") or {}
        if isinstance(leaf, str) and leaf in mapping:
            return leaf
        if isinstance(mapping, dict):
            for node_id, node in reversed(list(mapping.items())):
                if not isinstance(node, dict):
                    continue
                if node.get("children"):
                    continue
                message = node.get("message")
                if isinstance(message, dict) and message.get("id"):
                    return str(message["id"])
            for node in reversed(list(mapping.values())):
                if isinstance(node, dict) and isinstance(node.get("message"), dict):
                    return str(node["message"].get("id") or "") or None
        return None

    def new_conversation(self):
        self._chat_id = None
        self._parent_msg_id = None
        self._history = []
        self._system_state = ""
        self._system_applied = ""

    @property
    def conversation_id(self) -> Optional[str]:
        return self._chat_id

    # ------------------------------------------------------------------
    # message building
    # ------------------------------------------------------------------
    @staticmethod
    def _content_text(content) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(
                p.get("text", "") for p in content
                if isinstance(p, dict) and p.get("type") in ("text", "input_text", None)
                and isinstance(p.get("text"), str)
            )
        return str(content or "")

    @staticmethod
    def _attachment_image_parts(messages: list) -> list:
        parts = []
        for msg in messages:
            if msg.get("role") != "user":
                continue
            content = msg.get("content")
            if isinstance(content, list):
                for p in content:
                    if isinstance(p, dict) and p.get("type") == "image_url":
                        parts.append(p)
        return parts

    @staticmethod
    def _flatten(messages: list, system_prompt: str = "") -> str:
        """Transcript-style prompt for a brand new conversation."""
        return flatten_messages(messages, system_prompt=system_prompt)

    def resolve_system(self, system: Optional[str] = None) -> str:
        """Per-call override wins over the client default; `""` disables."""
        raw = self.system_prompt if system is None else system
        return (raw or "").strip()

    def _outgoing_text(self, messages: list, pin: bool, system: Optional[str] = None) -> str:
        """
        Text that goes out for this turn.

        Fresh conversations flatten the transcript (instructions included);
        pinned turns send only the newest message, so the instructions ride
        along just once per conversation - or again after they change.
        `self._system_applied` records what this text carried; `_do_stream`
        commits it to `_system_state` only once the turn succeeds.
        """
        effective = self.resolve_system(system)
        self._system_applied = self._system_state
        if not pin:
            text = self._flatten(messages, system_prompt=effective)
            has_explicit = any(
                m.get("role") in ("system", "developer") for m in messages
            )
            if effective and not has_explicit:
                self._system_applied = effective
            return text
        text = "Hello"
        for msg in reversed(messages):
            if msg.get("role") == "user":
                text = self._content_text(msg.get("content", "")) or "Hello"
                break
        if effective and self._system_state != effective:
            text = apply_system_prompt(text, effective)
            self._system_applied = effective
        return text

    async def upload_attachment(self, source, filename: Optional[str] = None) -> dict:
        """
        Upload a file through POST /backend-api/files (login required).

        source: local path, http(s) URL or data: URI.
        Returns {"part": content part, "file": attachment metadata}.
        """
        remote_url = None
        data = None
        name = filename

        if isinstance(source, dict) and source.get("type") in ("image_url", "file_url", "video_url"):
            url = (source.get(source["type"]) or {}).get("url") or ""
            if url.startswith("data:") or (
                url.startswith(("http://", "https://")) and not url.startswith(self.BASE_URL + "/")
            ):
                return await self.upload_attachment(url)
            return {"part": source, "file": {}}

        if isinstance(source, str) and source.startswith(("http://", "https://")):
            remote_url = source
            name = name or source.rsplit("/", 1)[-1].split("?")[0] or "file"
            async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as s:
                async with s.get(remote_url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                    if resp.status != 200:
                        raise AttachmentError(f"could not download {remote_url}: {resp.status}")
                    data = await resp.read()
        elif isinstance(source, str) and source.startswith("data:"):
            header, _, payload = source.partition(",")
            if ";base64" in header:
                data = base64.b64decode(payload)
            else:
                from urllib.parse import unquote_to_bytes
                data = unquote_to_bytes(payload)
            mime = header[5:].split(";")[0] if header.startswith("data:") else ""
            ext = mimetypes.guess_extension(mime) if mime else None
            name = name or f"attachment{ext or ''}"
        else:
            path = str(source)
            with open(path, "rb") as f:
                data = f.read()
            name = name or os.path.basename(path)

        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        meta = {
            "file_name": name,
            "file_size": len(data),
            "use_case": "multimodal",
            "mime_type": mime,
            "timezone_offset_min": _timezone_offset_min(),
            "reset_rate_limits": False,
            "supports_direct_azure_multipart": True,
        }
        await self._ensure_session()
        async with self._session.post(
            f"{self.API_URL}/files",
            data=json.dumps(meta, separators=(",", ":")),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            timeout=aiohttp.ClientTimeout(total=60),
        ) as resp:
            text = await resp.text()
            if resp.status in (401, 403):
                raise AttachmentError(
                    f"attachment upload rejected {resp.status}: {text[:300]} - login required "
                    "(python -m gptpp.login)"
                )
            if resp.status != 200:
                raise AttachmentError(f"attachment upload failed {resp.status}: {text[:300]}")
            try:
                body = json.loads(text)
            except json.JSONDecodeError as e:
                raise AttachmentError(f"attachment upload bad response: {text[:300]}") from e

        file_id = str(body.get("id") or body.get("file_id") or "")
        upload_url = (
            body.get("upload_url")
            or body.get("azure_upload_url")
            or body.get("direct_url")
            or body.get("sas_url")
            or ""
        )
        if upload_url and data is not None:
            async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as s:
                async with s.put(
                    upload_url, data=data, headers={"Content-Type": mime},
                    timeout=aiohttp.ClientTimeout(total=120),
                ) as up:
                    if up.status >= 400:
                        raise AttachmentError(f"attachment transfer failed {up.status}")
        elif not file_id:
            raise AttachmentError(f"attachment upload returned no id/url: {text[:300]}")

        image = mime.startswith("image/")
        part = {
            "content_type": "image_asset_pointer" if image else "file_asset_pointer",
            "asset_pointer": f"file-{file_id}",
        }
        file_meta = {"id": file_id, "name": name, "mimeType": mime, "size": len(data)}
        if remote_url:
            file_meta["url"] = remote_url
        return {"part": part, "file": file_meta}

    async def _resolve_attachments(self, attachments: list) -> tuple[list, list]:
        parts: list = []
        metas: list = []
        for a in attachments:
            res = await self.upload_attachment(a)
            parts.append(res["part"])
            if res.get("file"):
                metas.append(res["file"])
        return parts, metas

    async def _build_request(self, messages: list, model: Optional[str],
                             opts: Optional[dict]) -> tuple[str, dict]:
        """(url, payload) for POST /backend-api/conversation."""
        opts = opts or {}
        pin = bool(self._chat_id and self._parent_msg_id)
        text = self._outgoing_text(messages, pin=pin, system=opts.get("system"))
        parent_id = self._parent_msg_id or str(uuid.uuid4())

        attachment_parts: list = []
        attachment_metas: list = []
        if opts.get("attachments"):
            attachment_parts, attachment_metas = await self._resolve_attachments(opts["attachments"])
        elif not pin:
            image_parts = self._attachment_image_parts(messages)
            if image_parts:
                # already-uploaded pointers pass through untouched
                attachment_parts = [
                    p.get("image_url") or p for p in image_parts
                    if isinstance(p, dict)
                ]

        content: dict = {"content_type": "text", "parts": [text]}
        metadata: dict = {}
        if attachment_parts:
            normalized = []
            for p in attachment_parts:
                if isinstance(p, dict) and "content_type" in p:
                    normalized.append(p)
                else:
                    url = (p or {}).get("url") or ""
                    normalized.append({
                        "content_type": "image_asset_pointer",
                        "asset_pointer": url or "",
                    })
            content = {
                "content_type": "multimodal_text",
                "parts": [{"content_type": "text", "text": text}] + normalized,
            }
            metadata["attachments"] = attachment_metas or [{}]

        msg_id = str(uuid.uuid4())
        now = time.time()
        user_message = {
            "id": msg_id,
            "author": {"role": "user"},
            "content": content,
            "content_type": content["content_type"],
            "status": "finished_successfully",
            "end_turn": None,
            "weight": 1.0,
            "metadata": metadata,
            "created_at": now,
            "parent_id": parent_id,
        }

        payload: dict = {
            "action": "next",
            "messages": [user_message],
            "model": model or DEFAULT_MODEL,
            "parent_message_id": parent_id,
            "timezone_offset_min": _timezone_offset_min(),
            "timezone": _local_tz_name(),
            "history_and_training_disabled": False,
            "is_do_not_remember": False,
            "supports_buffering": True,
            "force_paragen": False,
            "force_rate_limit": False,
            "reset_rate_limits": False,
            "record_rendering": False,
            "client_prepare_state": "none",
        }
        if self._chat_id:
            payload["conversation_id"] = self._chat_id

        if opts.get("web_search"):
            payload["force_use_search"] = True
        effort = opts.get("reasoning_effort") or opts.get("thinking_effort")
        if effort and opts.get("deep_think", True):
            payload["thinking_effort"] = effort
        if attachment_metas:
            payload["attachment_mime_types"] = [m.get("mimeType") for m in attachment_metas if m.get("mimeType")]

        return f"{self.API_URL}/conversation", payload

    # ------------------------------------------------------------------
    # streaming
    # ------------------------------------------------------------------
    @staticmethod
    def _unwrap(frame: Any) -> dict:
        if not isinstance(frame, dict):
            return {}
        inner = frame.get("p")
        # {"v":1,"p":{...}} envelopes - and the occasional bare {"p":{...}}
        if isinstance(inner, dict) and "message" not in frame:
            return inner
        return frame

    async def _iter_frames(self, resp) -> AsyncGenerator[dict, None]:
        buffer = ""
        async for raw in resp.content:
            buffer += raw.decode("utf-8", errors="ignore")
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data:
                    continue
                if data == "[DONE]":
                    yield {"__done": True}
                    return
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue
                yield self._unwrap(obj)

    async def _stream_once(self, messages: list, timeout: float, model: Optional[str],
                           opts: dict) -> AsyncGenerator[tuple[str, str], None]:
        """
        One attempt: chat-requirements -> POST /conversation -> frame walk.

        Yields (phase, delta) with phase in {"thinking", "answer"}.
        """
        url, payload = await self._build_request(messages, model, opts)
        requirements = await self.refresh_requirements()
        sent_user_id = payload["messages"][0]["id"]
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        headers = requirements.headers(
            turnstile_token=self._turnstile_token,
            proof_token=self._proof_token,
            sentinel_token=self._sentinel_token,
        )

        await self._ensure_session()
        seen: dict[str, int] = {}
        last_message_id = sent_user_id
        done = False

        async with self._session.post(
            url,
            data=body,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            if resp.status != 200:
                err = await resp.text()
                if resp.status in (401, 403) and "authentication" in err.lower():
                    raise AuthError(f"chatgpt.com rejected the token ({resp.status}): {err[:300]}")
                raise UpstreamError(f"conversation failed {resp.status}: {err[:400]}",
                                    status=resp.status, body=err)

            async for frame in self._iter_frames(resp):
                if frame.get("__done"):
                    done = True
                    break
                if frame.get("error"):
                    raise UpstreamError(f"stream error: {frame['error']}")
                if frame.get("type") == "message_stream_complete":
                    done = True
                    break
                if frame.get("conversation_id"):
                    frame_chat = str(frame["conversation_id"])
                    if frame_chat != self._chat_id:
                        self._chat_id = frame_chat
                        # a new conversation id starts the instructions over
                        self._system_state = ""

                message = frame.get("message")
                if not isinstance(message, dict):
                    continue
                msg_id = str(message.get("id") or "")
                if msg_id:
                    last_message_id = msg_id
                author = message.get("author") or {}
                role = str(author.get("role") or "")
                content = message.get("content") or {}
                content_type = str(content.get("content_type") or "text")
                parts = content.get("parts") or []
                text = "".join(p for p in parts if isinstance(p, str))

                if content_type in ("reasoning_text", "thinking") or role in ("reasoning", "analysis"):
                    phase = "thinking"
                elif role in ("tool", "system"):
                    continue
                else:
                    phase = "answer"

                key = msg_id or phase
                prev = seen.get(key, 0)
                if len(text) < prev:
                    prev = 0
                delta = text[prev:]
                seen[key] = len(text)
                if delta:
                    yield phase, delta

                metadata = message.get("metadata") or {}
                finish = (metadata.get("finish_details") or {}).get("type")
                if message.get("status") == "finished_successfully" and finish in (
                    "stop", "max_tokens", "interrupted", "content_filter",
                ):
                    done = True

        if not done:
            # stream closed without an explicit finish frame - treat as complete
            pass
        self._parent_msg_id = last_message_id

    async def _mweb_stream(self, messages: list, timeout: float,
                            system: Optional[str] = None) -> AsyncGenerator[str, None]:
        """Replay through the anonymous `/unauth-mweb` flow (pure HTTP)."""
        self._mweb_final = ""
        if self._mweb is None:
            self._mweb = MwebChatClient()
            await self._mweb.open()
        if self._mweb_chat_id != self._chat_id:
            # a different /backend-api chat id starts a fresh mweb conversation
            self._mweb.new_conversation()
            self._mweb_chat_id = self._chat_id
        effective = self.resolve_system(system)
        if any(m.get("role") in ("system", "developer") for m in messages):
            effective = ""  # explicit system turns travel inside the flatten
        if self._mweb.conversation_state.get("userMessageCount"):
            # the mweb conversation already holds the earlier turns - only the
            # newest message goes over the wire
            text = self._content_text(messages[-1].get("content", "")) if messages else ""
        else:
            text = ""
        if not text:
            text = self._flatten(messages, system_prompt=effective)
        async for chunk in self._mweb.send_message_stream(
            text, timeout=max(timeout, 180), system=effective
        ):
            if chunk:
                yield chunk
        # parser's final answer is authoritative (deltas can carry stale
        # pending-preview text across a pending -> committed swap)
        self._mweb_final = self._mweb.last_turn.get("answer", "") or ""

    async def _browser_stream(self, messages: list, model: Optional[str],
                              opts: dict) -> AsyncGenerator[str, None]:
        """Replay through the Playwright client (sentinel/turnstile native)."""
        if self._browser is None:
            from .client import ChatGptClient
            self._browser = ChatGptClient(headless=True)
            await self._browser.start()
        text = self._outgoing_text(messages, pin=False, system=opts.get("system"))
        async for chunk in self._browser.send_message_stream(text, timeout=180):
            if chunk:
                yield chunk

    def _compose_reply(self, chunks: list) -> str:
        """Prefer the mweb parser's authoritative final answer when it ran."""
        return (self._mweb_final or "".join(chunks))

    async def _do_stream(self, messages: list, timeout: float = 120,
                         chat_id: Optional[str] = None, model: Optional[str] = None,
                         opts: Optional[dict] = None) -> AsyncGenerator[str, None]:
        opts = opts or {}
        self._mweb_final = ""
        await self._ensure_session()

        if chat_id is not None and chat_id != self._chat_id:
            self._chat_id = chat_id
            self._parent_msg_id = None
            self._system_state = ""
            self._system_applied = ""
        if self._chat_id and self._parent_msg_id is None:
            self._parent_msg_id = await self._fetch_leaf(self._chat_id)

        # each attempt re-mints chat-requirements (~seconds of PoW) - anonymous
        # sessions cannot win /backend-api enforcement anyway, so go to the
        # mweb fallback after a single attempt
        fallback = self.mweb_fallback or self.browser_fallback
        max_attempts = 1 if (fallback and not self.logged_in) else 3
        last_error: Optional[Exception] = None
        for attempt in range(1, max_attempts + 1):
            try:
                async for phase, delta in self._stream_once(messages, timeout, model, opts):
                    if phase == "answer":
                        yield delta
                self._system_state = self._system_applied
                return
            except SentinelError as e:
                last_error = e
                if not fallback:
                    raise
                break
            except AuthError as e:
                last_error = e
                if self.logged_in and attempt < max_attempts:
                    print("[api] session expired - refreshing access token...", flush=True)
                    await self.refresh_session()
                    continue
                break
            except UpstreamError as e:
                last_error = e
                status = e.status
                if status == 401 and self.logged_in and attempt < max_attempts:
                    print("[api] 401 - refreshing access token...", flush=True)
                    await self.refresh_session()
                    continue
                if status == 403 and attempt < max_attempts:
                    print("[api] 403 sentinel enforcement - refreshing chat-requirements...", flush=True)
                    self._requirements = None
                    await self.refresh_requirements(force=True)
                    continue
                break

        if not fallback:
            if isinstance(last_error, UpstreamError) and last_error.status == 403:
                raise SentinelError(
                    "chatgpt.com sentinel enforcement rejected the pure-HTTP "
                    "request (403 'Unusual activity'). Enable mweb_fallback "
                    "(default) for an anonymous pure-HTTP replay, run "
                    "`python -m gptpp.login`, or construct "
                    "ChatGptApiClient(browser_fallback=True)."
                ) from last_error
            if last_error is not None:
                raise last_error
            raise SentinelError(
                "chatgpt.com conversation request failed with no error captured"
            )

        mweb_error: Optional[Exception] = None
        if self.mweb_fallback:
            try:
                if not self._mweb_notice:
                    self._mweb_notice = True
                    print("[api] backend-api unavailable - replaying over pure-HTTP "
                          "/unauth-mweb", flush=True)
                async for chunk in self._mweb_stream(
                    messages, timeout, system=opts.get("system")
                ):
                    yield chunk
                return
            except (AuthError, UpstreamError, SentinelError) as e:
                mweb_error = e
                print(f"[api] unauth-mweb replay failed: {e}", flush=True)

        if self.browser_fallback:
            print("[api] falling back to the Playwright browser client", flush=True)
            async for chunk in self._browser_stream(messages, model, opts):
                yield chunk
            return

        if mweb_error is not None:
            raise mweb_error
        raise SentinelError(
            "chatgpt.com blocked the pure-HTTP conversation request and no "
            "fallback transport succeeded. Run `python -m gptpp.client` for "
            "the REPL, or construct ChatGptApiClient(browser_fallback=True)."
        ) from last_error

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------
    async def send_message_stream(
        self,
        message: str,
        timeout: float = 120,
        chat_id: Optional[str] = None,
        use_history: bool = True,
        model: Optional[str] = None,
        web_search: bool = False,
        deep_think: bool = True,
        reasoning_effort: Optional[str] = None,
        attachments: Optional[list] = None,
        system: Optional[str] = None,
    ) -> AsyncGenerator[str, None]:
        if use_history and self._history and self._history[-1] == {"role": "user", "content": message}:
            messages = list(self._history)
        elif use_history:
            messages = [*self._history, {"role": "user", "content": message}]
        else:
            messages = [{"role": "user", "content": message}]
        opts = {
            "web_search": bool(web_search),
            "deep_think": bool(deep_think),
            "reasoning_effort": reasoning_effort,
            "attachments": list(attachments or []),
            "system": system,
        }
        async for chunk in self._do_stream(messages, timeout, chat_id=chat_id, model=model, opts=opts):
            yield chunk

    async def send_message(
        self,
        message: str,
        timeout: float = 120,
        chat_id: Optional[str] = None,
        model: Optional[str] = None,
        web_search: bool = False,
        deep_think: bool = True,
        reasoning_effort: Optional[str] = None,
        attachments: Optional[list] = None,
        system: Optional[str] = None,
    ) -> str:
        self._history.append({"role": "user", "content": message})
        chunks = []
        async for chunk in self.send_message_stream(
            message, timeout, chat_id=chat_id, model=model, web_search=web_search,
            deep_think=deep_think, reasoning_effort=reasoning_effort,
            attachments=attachments, system=system,
        ):
            chunks.append(chunk)
        reply = self._compose_reply(chunks)
        if reply:
            self._history.append({"role": "assistant", "content": reply})
        return reply

    async def send_messages_stream(
        self,
        messages: list,
        timeout: float = 120,
        chat_id: Optional[str] = None,
        model: Optional[str] = None,
        web_search: bool = False,
        deep_think: bool = True,
        reasoning_effort: Optional[str] = None,
        attachments: Optional[list] = None,
        system: Optional[str] = None,
    ) -> AsyncGenerator[str, None]:
        opts = {
            "web_search": bool(web_search),
            "deep_think": bool(deep_think),
            "reasoning_effort": reasoning_effort,
            "attachments": list(attachments or []),
            "system": system,
        }
        async for chunk in self._do_stream(messages, timeout, chat_id=chat_id, model=model, opts=opts):
            yield chunk

    async def send_messages(
        self,
        messages: list,
        timeout: float = 120,
        chat_id: Optional[str] = None,
        model: Optional[str] = None,
        web_search: bool = False,
        deep_think: bool = True,
        reasoning_effort: Optional[str] = None,
        attachments: Optional[list] = None,
        system: Optional[str] = None,
    ) -> str:
        chunks = []
        async for chunk in self.send_messages_stream(
            messages, timeout, chat_id=chat_id, model=model, web_search=web_search,
            deep_think=deep_think, reasoning_effort=reasoning_effort,
            attachments=attachments, system=system,
        ):
            chunks.append(chunk)
        reply = self._compose_reply(chunks)
        self._history = list(messages)
        if reply:
            self._history.append({"role": "assistant", "content": reply})
        return reply

    async def send_message_full(
        self,
        message: str,
        timeout: float = 120,
        chat_id: Optional[str] = None,
        model: Optional[str] = None,
        web_search: bool = False,
        deep_think: bool = True,
        reasoning_effort: Optional[str] = None,
        attachments: Optional[list] = None,
        system: Optional[str] = None,
    ) -> dict:
        """Buffered call returning {"thinking": ..., "response": ...}."""
        messages = [*self._history, {"role": "user", "content": message}]
        opts = {
            "web_search": bool(web_search),
            "deep_think": bool(deep_think),
            "reasoning_effort": reasoning_effort,
            "attachments": list(attachments or []),
            "system": system,
        }
        thinking: list[str] = []
        answer: list[str] = []
        await self._ensure_session()
        if chat_id is not None and chat_id != self._chat_id:
            self._chat_id = chat_id
            self._parent_msg_id = None
            self._system_state = ""
            self._system_applied = ""
        if self._chat_id and self._parent_msg_id is None:
            self._parent_msg_id = await self._fetch_leaf(self._chat_id)

        for attempt in range(1, 4):
            thinking, answer = [], []
            try:
                async for phase, delta in self._stream_once(messages, timeout, model, opts):
                    (thinking if phase == "thinking" else answer).append(delta)
                break
            except AuthError:
                if attempt >= 3:
                    raise
                await self.refresh_session()
            except UpstreamError as e:
                if e.status == 401 and attempt < 3:
                    await self.refresh_session()
                elif e.status == 403 and attempt < 3:
                    self._requirements = None
                    await self.refresh_requirements(force=True)
                else:
                    raise
        else:
            raise SentinelError("chatgpt.com sentinel enforcement kept rejecting the request")

        self._system_state = self._system_applied
        self._history.append({"role": "user", "content": message})
        text = self._compose_reply(answer)
        if text:
            self._history.append({"role": "assistant", "content": text})
        return {"thinking": "".join(thinking), "response": text}
