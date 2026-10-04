#!/usr/bin/env python3
"""
chatgpt.com browser client (async).

Playwright Firefox drives the real page - its own sentinel/turnstile headers
are what actually go out - and we read the answer back two ways:

  * in-page fetch interception for `/backend-api/conversation` SSE (the
    logged-in desktop app), and
  * DOM polling of the transcript + `data-conversation-control` markers,
    which is what the logged-out mobile-web variant
    (`/unauth-mweb/conversation/updates`, declarative-partial-update HTML)
    renders into.

The REPL itself defaults to plain HTTP (gptpp/mweb.py) and only needs
Playwright when you ask for it:

    python -m gptpp.client            # interactive chat REPL (pure HTTP)
    python -m gptpp.client --browser  # the same REPL through Firefox
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import time
from dataclasses import dataclass
from typing import AsyncGenerator, List, Optional

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".chatgpt_browser_state.json")

FIREFOX_UA = "Mozilla/5.0 (X11; Linux x86_64; rv:141.0) Gecko/20100101 Firefox/141.0"

COMPOSER_SELECTORS = [
    "#prompt-textarea",
    "div[contenteditable='true']",
    "[data-testid='composer-textarea']",
    "textarea",
]

SEND_BUTTON_SELECTORS = [
    "button[data-testid='send-button']",
    "button[data-testid='composer-send-button']",
    "button[aria-label='Send prompt']",
    "button[aria-label='Send message']",
]

ASSISTANT_SELECTORS = [
    'li[data-message-role="assistant"]',
    '[data-message-role="assistant"]',
    '[data-message-author-role="assistant"]',
    'article[data-testid="conversation-turn-assistant"]',
]

REASONING_SELECTORS = [
    '[data-message-role="reasoning"]',
    '[data-message-author-role="reasoning"]',
    '[data-message-author-role="analysis"]',
]

# `data-conversation-control` spans the mobile-web DPU stream appends when a
# turn ends (verified live); the desktop app never emits them, so `sse_done`
# below covers that path instead.
DONE_MARKERS = [
    '[data-conversation-control="message-stream-complete"]',
    '[data-conversation-control="terminal-received"]',
    '[data-conversation-control="complete"]',
]

_ATTRIBUTION_RE = re.compile(r"^\s*(?:ChatGPT|You) said:\s*", re.IGNORECASE)
_UC_PATH_RE = re.compile(r"/uc/([0-9a-fA-F-]{8,})", re.IGNORECASE)

_SSE_INTERCEPTOR = """
(() => {
    window.__gpt_sse = { thinking: '', answer: '', done: false, conversation_id: '', dpu: false, error: '' };
    const _origFetch = window.fetch;
    const streamTypes = ['event-stream', 'text/plain', 'vnd.openai.web-mobile-partial'];

    window.fetch = async function(...args) {
        const resp = await _origFetch.apply(this, args);
        const url = typeof args[0] === 'string' ? args[0] : (args[0]?.url || '');
        if (!/\\/conversation(\\/|\\?|$)/.test(url)) return resp;
        const ct = (resp.headers.get('content-type') || '').toLowerCase();
        if (!streamTypes.some(t => ct.includes(t))) return resp;
        if (!resp.body) return resp;
        if (ct.includes('vnd.openai')) {
            // mobile-web declarative partial updates: no data: lines to parse,
            // the DOM is the source of truth - just note that we saw it
            window.__gpt_sse.dpu = true;
            return resp;
        }

        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        window.__gpt_sse = { thinking: '', answer: '', done: false, conversation_id: '', dpu: false, error: '' };

        const parse = (obj) => {
            if (!obj || typeof obj !== 'object') return;
            if (obj.p && typeof obj.p === 'object' && 'message' in obj.p === false) obj = obj.p;
            if (obj.error) { window.__gpt_sse.done = true; window.__gpt_sse.error = JSON.stringify(obj.error); return; }
            if (obj.type === 'message_stream_complete') { window.__gpt_sse.done = true; return; }
            if (obj.conversation_id) window.__gpt_sse.conversation_id = obj.conversation_id;
            const m = obj.message;
            if (!m || typeof m !== 'object') return;
            const content = m.content || {};
            const parts = content.parts || [];
            const text = parts.filter(p => typeof p === 'string').join('');
            const role = (m.author && m.author.role) || '';
            const thinking = content.content_type === 'reasoning_text'
                || content.content_type === 'thinking'
                || role === 'reasoning' || role === 'analysis';
            if (thinking) window.__gpt_sse.thinking = text;
            else if (role !== 'tool' && role !== 'system') window.__gpt_sse.answer = text;
            const fin = (m.metadata && m.metadata.finish_details && m.metadata.finish_details.type) || '';
            if (m.status === 'finished_successfully' && fin) window.__gpt_sse.done = true;
        };

        const stream = new ReadableStream({
            start(controller) {
                (function pump() {
                    reader.read().then(({ done, value }) => {
                        if (done) { window.__gpt_sse.done = true; controller.close(); return; }
                        buffer += decoder.decode(value, { stream: true });
                        const lines = buffer.split('\\n');
                        buffer = lines.pop();
                        for (const line of lines) {
                            const trimmed = line.trim();
                            if (!trimmed.startsWith('data:')) continue;
                            const payload = trimmed.slice(5).trim();
                            if (!payload) continue;
                            if (payload === '[DONE]') { window.__gpt_sse.done = true; continue; }
                            try { parse(JSON.parse(payload)); } catch (e) {}
                        }
                        controller.enqueue(value);
                        pump();
                    }).catch(() => {
                        window.__gpt_sse.done = true;
                        try { controller.close(); } catch (e) {}
                    });
                })();
            }
        });
        return new Response(stream, { status: resp.status, statusText: resp.statusText, headers: resp.headers });
    };
})();
"""

_STATE_JS = """
() => {
    const sse = window.__gpt_sse || {};
    const last = (sels) => {
        for (const sel of sels) {
            const nodes = document.querySelectorAll(sel);
            if (nodes.length) return nodes[nodes.length - 1];
        }
        return null;
    };
    const clean = (el) => {
        const c = el.cloneNode(true);
        c.querySelectorAll(
            'style, script, noscript, svg, audio, h4[data-message-attribution], ' +
            '[role="menu"], [popover], [data-user-message-actions], [data-message-actions], ' +
            'button[aria-haspopup="menu"]'
        ).forEach(n => n.remove());
        return (c.innerText || '').trim();
    };
    const assistant = last(%ASSISTANT%);
    const reasoning = last(%REASONING%);
    const doneEls = %DONE%.reduce((acc, sel) => acc.concat([...document.querySelectorAll(sel)]), []);
    const seen = window.__gpt_done_ids || [];
    const ids = doneEls.map(e => e.getAttribute('data-operation-id') || e.getAttribute('data-message-id') || '');
    const domDone = ids.length > (seen.length || 0)
        ? ids.some(id => !id || seen.indexOf(id) === -1)
        : false;
    const cidEl = document.querySelector('[data-conversation-control="conversation-id"]');
    const path = location.pathname || '';
    const pathMatch = path.match(/\\/uc\\/([0-9a-fA-F-]{8,})/);
    return {
        sse_answer: sse.answer || '',
        sse_thinking: sse.thinking || '',
        sse_done: !!sse.done,
        sse_cid: sse.conversation_id || '',
        sse_error: sse.error || '',
        answer: assistant ? clean(assistant) : '',
        reasoning_id: reasoning ? (reasoning.getAttribute('id') || '') : '',
        assistant_id: assistant ? (assistant.getAttribute('id') || '') : '',
        thinking: reasoning ? clean(reasoning) : '',
        dom_done: domDone,
        cid: (cidEl && cidEl.getAttribute('data-conversation-id')) || (pathMatch ? pathMatch[1] : ''),
        turn_count: document.querySelectorAll('[data-message-role="assistant"],[data-message-author-role="assistant"]').length,
    };
}
""".replace("%ASSISTANT%", json.dumps(ASSISTANT_SELECTORS))

_STATE_JS = _STATE_JS.replace("%REASONING%", json.dumps(REASONING_SELECTORS)).replace(
    "%DONE%", json.dumps(DONE_MARKERS)
)


@dataclass
class ChatMessage:
    role: str
    content: str


class ChatGptClient:
    def __init__(self, headless: bool = True, stall_timeout: float = 8.0):
        self.headless = headless
        self.stall_timeout = stall_timeout
        self._pending_cid = ""
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self._playwright = None

    async def start(self) -> None:
        await self.close()
        self._playwright = await async_playwright().start()
        self.browser = await self._playwright.firefox.launch(
            headless=self.headless,
            firefox_user_prefs={
                "general.useragent.override": FIREFOX_UA,
                "dom.webdriver.enabled": False,
                "useAutomationExtension": False,
            },
        )
        self.context = await self.browser.new_context(
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
            user_agent=FIREFOX_UA,
            storage_state=STATE_FILE if os.path.exists(STATE_FILE) else None,
        )
        await self.context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
        )
        await self.context.add_init_script(_SSE_INTERCEPTOR)
        self.page = await self.context.new_page()
        await self.page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=60000)
        await self._wait_for_composer()

    async def _wait_for_composer(self, timeout: int = 60000) -> None:
        editor = await self._find_element(COMPOSER_SELECTORS, timeout=timeout)
        if not editor:
            raise RuntimeError(
                "chatgpt.com composer not found - the layout may have changed, "
                "or the page is blocked/consent-walled"
            )

    async def wait_for_auth(self) -> Optional[str]:
        await asyncio.to_thread(input, "Log in to chatgpt.com in the window, then press ENTER...")
        token = ""
        try:
            resp = await self.page.request.get("https://chatgpt.com/api/auth/session")
            if resp.ok:
                token = (await resp.json()).get("accessToken") or ""
        except Exception:
            token = ""
        try:
            if self.context:
                await self.context.storage_state(path=STATE_FILE)
        except OSError:
            pass
        return token

    # ------------------------------------------------------------------
    # state
    # ------------------------------------------------------------------
    async def _state(self) -> dict:
        return await self.page.evaluate(_STATE_JS)

    async def _reset_state(self) -> None:
        await self.page.evaluate(
            """
            () => {
                window.__gpt_sse = { thinking: '', answer: '', done: false,
                                      conversation_id: '', dpu: false, error: '' };
                const sels = @@LIST@@;
                const ids = [];
                for (const sel of sels) {
                    for (const el of document.querySelectorAll(sel)) {
                        ids.push(el.getAttribute('data-operation-id') || el.getAttribute('data-message-id') || '');
                    }
                }
                window.__gpt_done_ids = ids;
            }
            """.replace("@@LIST@@", json.dumps(DONE_MARKERS))
        )

    @staticmethod
    def _strip_attribution(text: str) -> str:
        """Drop the transcript labels ('ChatGPT said:' / 'You said:')."""
        text = (text or "").strip()
        text = _ATTRIBUTION_RE.sub("", text, count=1)
        return text.strip()

    # ------------------------------------------------------------------
    # sending
    # ------------------------------------------------------------------
    async def _auth_dialog_present(self) -> bool:
        """The logged-out quota popup (#mobile-auth-dialog) or any visible modal."""
        try:
            return bool(
                await self.page.evaluate(
                    """() => {
                        const sels = ['#mobile-auth-dialog', 'dialog[open]',
                                       '[data-testid="login-modal"]',
                                       '[aria-labelledby="mobile-auth-title"]'];
                        for (const sel of sels) {
                            for (const el of document.querySelectorAll(sel)) {
                                const visible = el.checkVisibility
                                    ? el.checkVisibility({ checkOpacity: false, checkVisibilityCSS: true })
                                    : (el.offsetParent !== null);
                                if (visible) return true;
                            }
                        }
                        return false;
                    }"""
                )
            )
        except Exception:
            return False

    async def _dismiss_dialogs(self) -> bool:
        """Close modal overlays (Escape dismisses chatgpt.com bottom sheets)."""
        if not await self._auth_dialog_present():
            return False
        for _ in range(3):
            try:
                await self.page.keyboard.press("Escape")
            except Exception:
                break
            await self.page.wait_for_timeout(250)
            if not await self._auth_dialog_present():
                return True
        return False

    async def _type_and_send(self, message: str) -> None:
        if await self._dismiss_dialogs():
            await self.page.wait_for_timeout(200)

        editor = await self._find_element(COMPOSER_SELECTORS)
        if not editor:
            raise RuntimeError("Could not find the chat composer")

        try:
            await editor.click(timeout=8000)
        except Exception:
            await self._dismiss_dialogs()
            editor = await self._find_element(COMPOSER_SELECTORS)
            if not editor:
                raise RuntimeError("Could not find the chat composer")
            await editor.click(timeout=8000)
        await self.page.wait_for_timeout(150 + random.randint(0, 100))

        filled = await self.page.evaluate(
            """
            (msg) => {
                const sels = @@LIST@@;
                let el = null;
                for (const sel of sels) {
                    const cand = document.querySelector(sel);
                    if (cand) { el = cand; break; }
                }
                if (!el) return false;
                el.focus();
                if (el.tagName === 'TEXTAREA') {
                    const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
                    setter.call(el, msg);
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    return true;
                }
                const selection = window.getSelection();
                const range = document.createRange();
                range.selectNodeContents(el);
                selection.removeAllRanges();
                selection.addRange(range);
                if (document.execCommand('insertText', false, msg)) return true;
                el.textContent = msg;
                el.dispatchEvent(new InputEvent('input', { bubbles: true, data: msg, inputType: 'insertText' }));
                return true;
            }
            """.replace("@@LIST@@", json.dumps(COMPOSER_SELECTORS)),
            message,
        )
        if not filled:
            raise RuntimeError("Could not populate the chat composer")

        await self.page.wait_for_timeout(200 + random.randint(0, 120))

        for selector in SEND_BUTTON_SELECTORS:
            try:
                btn = await self.page.query_selector(selector)
                if btn and await btn.is_enabled():
                    await btn.click(timeout=5000)
                    return
            except Exception:
                continue
        try:
            await self.page.keyboard.press("Enter")
        except Exception:
            pass
        if await self._auth_dialog_present():
            raise RuntimeError(
                "chatgpt.com is showing its login dialog (logged-out quota reached) - "
                "run: python -m gptpp.login"
            )

    async def _stream_internal(
        self,
        message: str,
        timeout: float = 180,
        include_thinking: bool = False,
    ) -> AsyncGenerator[tuple[str, str], None]:
        """
        Send `message` and yield (phase, delta) with phase in
        {"thinking", "answer"} - answer deltas only unless include_thinking.
        """
        if not self.page:
            raise RuntimeError("Client not started. Call start() first.")

        await self._reset_state()
        self._pending_cid = ""

        # baseline the transcript: later turns must not replay the previous
        # answer as if it were this turn's output (each turn renders into its
        # own node, so we track that node's id)
        try:
            st0 = await self._state()
        except Exception:
            st0 = {}
        cur_node = st0.get("assistant_id") or ""
        cur_reason = st0.get("reasoning_id") or ""
        answer = self._strip_attribution(st0.get("answer", "")) or st0.get("sse_answer", "")
        thinking = st0.get("thinking", "") or st0.get("sse_thinking", "")

        await self._type_and_send(message)

        started = time.time()
        last_change = time.time()
        last_dialog_check = 0.0
        yielded = 0
        done = False

        while time.time() - started < timeout:
            try:
                st = await self._state()
            except Exception:
                await asyncio.sleep(0.15)
                continue

            if st.get("sse_error"):
                raise RuntimeError(f"stream error: {st['sse_error']}")

            node = st.get("assistant_id") or ""
            reason = st.get("reasoning_id") or ""
            if node and node != cur_node:
                # a fresh assistant node means this turn's reply started
                cur_node = node
                answer = ""
                thinking = ""
            if reason and reason != cur_reason:
                cur_reason = reason
                thinking = ""

            new_answer = self._strip_attribution(st.get("answer", "")) or st.get("sse_answer", "")
            new_thinking = st.get("thinking", "") or st.get("sse_thinking", "")

            if new_thinking.startswith(thinking) and len(new_thinking) > len(thinking):
                delta = new_thinking[len(thinking):]
                thinking = new_thinking
            elif new_thinking and new_thinking != thinking:
                thinking = new_thinking
                delta = new_thinking
            else:
                delta = ""
            if delta:
                yielded += len(delta)
                last_change = time.time()
                if include_thinking:
                    yield "thinking", delta

            if new_answer.startswith(answer) and len(new_answer) > len(answer):
                delta = new_answer[len(answer):]
                answer = new_answer
            elif new_answer and new_answer != answer and not new_answer.startswith(answer):
                answer = new_answer
                delta = new_answer
            else:
                delta = ""
            if delta:
                yielded += len(delta)
                last_change = time.time()
                yield "answer", delta

            if st.get("cid") and not self._pending_cid:
                self._pending_cid = st["cid"]

            if st.get("sse_done") or st.get("dom_done"):
                done = True
                break
            # last resort: new text arrived but no completion marker showed up
            if yielded > 0 and time.time() - last_change > self.stall_timeout:
                done = True
                break

            if time.time() - last_dialog_check > 1.0:
                last_dialog_check = time.time()
                if yielded <= 0 and await self._auth_dialog_present():
                    raise RuntimeError(
                        "chatgpt.com is showing its login dialog (logged-out quota reached) - "
                        "run: python -m gptpp.login"
                    )

            await asyncio.sleep(0.2)

        if not done and yielded <= 0:
            if await self._auth_dialog_present():
                raise RuntimeError(
                    "chatgpt.com is showing its login dialog (logged-out quota reached) - "
                    "run: python -m gptpp.login"
                )
            raise RuntimeError(
                "no reply received within the timeout - chatgpt.com may be "
                "showing a login wall or layout changed (try: python -m gptpp.login)"
            )

        # one last read so late-arriving text isn't dropped
        try:
            st = await self._state()
            node = st.get("assistant_id") or ""
            if node and node != cur_node:
                answer = ""
            new_answer = self._strip_attribution(st.get("answer", "")) or st.get("sse_answer", "")
            new_thinking = st.get("thinking", "") or st.get("sse_thinking", "")
            if include_thinking and new_thinking.startswith(thinking) and len(new_thinking) > len(thinking):
                yield "thinking", new_thinking[len(thinking):]
            elif include_thinking and new_thinking and new_thinking != thinking:
                yield "thinking", new_thinking
            if new_answer.startswith(answer) and len(new_answer) > len(answer):
                yield "answer", new_answer[len(answer):]
            elif new_answer and new_answer != answer and not new_answer.startswith(answer):
                yield "answer", new_answer
            if st.get("cid"):
                self._pending_cid = st["cid"]
        except Exception:
            pass

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------
    async def send_message(self, message: str, timeout: float = 180) -> str:
        parts = [
            delta async for phase, delta in self._stream_internal(message, timeout)
            if phase == "answer"
        ]
        return "".join(parts)

    async def send_message_full(self, message: str, timeout: float = 180) -> dict:
        thinking: List[str] = []
        answer: List[str] = []
        self._pending_cid = ""
        async for phase, delta in self._stream_internal(message, timeout, include_thinking=True):
            (thinking if phase == "thinking" else answer).append(delta)
        return {
            "thinking": "".join(thinking),
            "response": "".join(answer),
            "conversation_id": self._pending_cid or self.conversation_id(),
        }

    async def send_message_stream(
        self,
        message: str,
        timeout: float = 180,
        include_thinking: bool = False,
    ) -> AsyncGenerator[str, None]:
        """Yield answer deltas (or 'thinking:'/'answer:' prefixed deltas)."""
        async for phase, delta in self._stream_internal(message, timeout, include_thinking=include_thinking):
            if not delta:
                continue
            yield (f"{phase}:{delta}") if include_thinking else delta

    async def send_message_stream_full(
        self, message: str, timeout: float = 180
    ) -> AsyncGenerator[tuple[str, str], None]:
        """Stream (phase, delta) tuples - phase is 'thinking' or 'answer'."""
        async for item in self._stream_internal(message, timeout, include_thinking=True):
            yield item

    def conversation_id(self) -> Optional[str]:
        """Conversation id last seen in the page (path or control span)."""
        return self._pending_cid or None

    # ------------------------------------------------------------------
    # reading
    # ------------------------------------------------------------------
    async def get_chat_history(self) -> List[ChatMessage]:
        try:
            result = await self.page.evaluate(
                """
                () => {
                    const msgs = [];
                    const clean = (el) => {
                        const c = el.cloneNode(true);
                        c.querySelectorAll(
                            'style, script, noscript, svg, audio, h4[data-message-attribution], ' +
                            '[role="menu"], [popover], [data-user-message-actions], [data-message-actions], ' +
                            'button[aria-haspopup="menu"]'
                        ).forEach(s => s.remove());
                        return (c.innerText || '').trim();
                    };
                    const nodes = [...document.querySelectorAll(
                        '[data-message-role="user"],[data-message-role="assistant"],' +
                        '[data-message-author-role="user"],[data-message-author-role="assistant"]'
                    )];
                    for (const el of nodes) {
                        const role = el.getAttribute('data-message-role')
                            || el.getAttribute('data-message-author-role');
                        let t = '';
                        if (role === 'user') {
                            const p = el.querySelector('[data-user-message-copy]');
                            t = p ? (p.innerText || '').trim() : '';
                        }
                        if (!t) t = clean(el).replace(/^\\s*(?:You|User|ChatGPT) said:\\s*/i, '').trim();
                        if (t) msgs.push({ role: role, content: t });
                    }
                    return msgs;
                }
                """
            )
            return [ChatMessage(m["role"], m["content"]) for m in result]
        except Exception:
            return []

    async def _extract_last_assistant_message_from_dom(self) -> str:
        st = await self._state()
        return self._strip_attribution(st.get("answer", ""))

    async def _find_element(self, selectors: List[str], timeout: int = 3000):
        per_selector = max(timeout // max(len(selectors), 1), 300)
        for sel in selectors:
            try:
                el = await self.page.wait_for_selector(sel, timeout=per_selector)
                if el and await el.is_visible():
                    return el
            except Exception:
                continue
        return None

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    async def close(self):
        try:
            if self.context:
                await self.context.storage_state(path=STATE_FILE)
        except Exception:
            pass
        try:
            if self.browser:
                await self.browser.close()
        except Exception:
            pass
        finally:
            self.browser = None
            self.context = None
            self.page = None
        try:
            if self._playwright:
                await self._playwright.stop()
        except Exception:
            pass
        finally:
            self._playwright = None

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, *args):
        await self.close()


async def _browser_repl(timeout: float = 180) -> None:
    """Interactive chat driven by Playwright Firefox."""
    client = ChatGptClient(headless=True)
    try:
        await client.start()
        await client.wait_for_auth()

        print("\n" + "=" * 60)
        print("CHAT STARTED (browser) - Type 'quit' to exit")
        print("=" * 60)

        while True:
            try:
                user_input = (await asyncio.to_thread(input, "\nYou: ")).strip()
                if not user_input:
                    continue
                if user_input.lower() in ("quit", "exit", "q"):
                    break
                if user_input.lower() in ("new", "reset"):
                    print("[conversation reset only works in the pure-HTTP REPL]")
                    continue

                print("Assistant: ", end="", flush=True)
                thinking_buf = ""
                answer_started = False
                async for phase, delta in client.send_message_stream_full(
                    user_input, timeout
                ):
                    if not delta:
                        continue
                    if phase == "thinking":
                        thinking_buf += delta
                        print(f"\r[thinking] {thinking_buf}", end="", flush=True)
                    else:
                        if not answer_started:
                            answer_started = True
                            print(f"\r{' ' * 78}\r", end="")
                        print(delta, end="", flush=True)
                print()
            except KeyboardInterrupt:
                print("\n\nGoodbye!")
                break
            except Exception as e:
                print(f"\n[Error] {e}")
    finally:
        await client.close()


async def _http_repl(timeout: float = 180) -> None:
    """Interactive chat over plain HTTP - no Playwright, no login."""
    from .errors import GptError
    from .mweb import MwebChatClient

    client = MwebChatClient()
    await client.open()
    print("\n" + "=" * 60)
    print("CHAT STARTED (pure HTTP) - 'new' resets, 'quit' exits")
    print("=" * 60)
    try:
        while True:
            try:
                user_input = (await asyncio.to_thread(input, "\nYou: ")).strip()
                if not user_input:
                    continue
                if user_input.lower() in ("quit", "exit", "q"):
                    break
                if user_input.lower() in ("new", "reset"):
                    client.new_conversation()
                    print("[conversation reset]")
                    continue

                print("Assistant: ", end="", flush=True)
                async for delta in client.send_message_stream(user_input, timeout):
                    print(delta, end="", flush=True)
                print()
            except KeyboardInterrupt:
                print("\n\nGoodbye!")
                break
            except GptError as e:
                print(f"\n[Error] {e}")
            except Exception as e:
                print(f"\n[Error] {e}")
    finally:
        await client.close()


async def main(argv: Optional[List[str]] = None) -> None:
    """Interactive chat with chatgpt.com (pure HTTP by default)."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m gptpp.client",
        description="Interactive chatgpt.com chat. Default transport is plain "
                    "HTTP (/unauth-mweb); --browser drives Playwright instead.",
    )
    parser.add_argument(
        "--browser",
        action="store_true",
        help="drive chatgpt.com with Playwright Firefox instead of pure HTTP",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=180,
        help="per-turn timeout in seconds (default: 180)",
    )
    args = parser.parse_args(argv)
    if args.browser:
        await _browser_repl(args.timeout)
    else:
        await _http_repl(args.timeout)


if __name__ == "__main__":
    asyncio.run(main())
