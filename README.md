# GPT4Free

![Python](https://img.shields.io/badge/python-3.10+-blue.svg)
![Async](https://img.shields.io/badge/async-supported-green.svg)
![License](https://img.shields.io/badge/license-MIT-green.svg)
![Status](https://img.shields.io/badge/status-experimental-yellow.svg)

Async chatgpt.com chat stack: three transports for the same `send_message*` API — a pure-HTTP `/backend-api` client, a pure-HTTP anonymous `/unauth-mweb` client (no login at all), and an async Playwright browser client with in-page SSE interception. Library only — no server, no OpenAI key.

> [!WARNING]
> Built on reverse-engineered chatgpt.com infrastructure. Endpoints, fingerprint config, and sentinel checks change without notice. Expect breakage.

> [!IMPORTANT]
> `POST /backend-api/conversation` enforces Cloudflare Turnstile / sentinel checks (403 `Unusual activity has been detected…`) for anonymous and often for logged-in sessions. `ChatGptApiClient` reacts by replaying the same prompt over the anonymous mobile-web endpoint (`POST /unauth-mweb/conversation/updates`) — still plain HTTP, no browser — and only falls through to Playwright when you also pass `browser_fallback=True`.

---

## Overview

* pure-HTTP `/backend-api` chatgpt.com client — chat-requirements prepare/finalize, FNV proof-of-work, SSE streaming, auto session refresh
* pure-HTTP `/unauth-mweb` client (`gptpp/mweb.py`) — sentinel + conduit token + declarative-partial-update HTML stream, works anonymously with **zero login** (this is the automatic fallback transport)
* async Playwright browser client with in-page SSE interception **and** transcript DOM polling
* `python -m gptpp.client` interactive REPL — pure HTTP by default, `--browser` for Playwright
* login helper that sniffs the bearer token + cookies into `.chatgpt_credentials.json`
* conversation continuation via `conversation_id` (pinned leaf node) or flattened history
* web search (`force_use_search`), deep think (`thinking_effort`), and attachments (`POST /backend-api/files`)
* transparent fallbacks: `/backend-api` → `/unauth-mweb` (default) → Playwright (`browser_fallback=True`)
* exceptions in `gptpp.errors`: `AuthError`, `UpstreamError`, `SentinelError`, `TurnstileRequiredError`, `AttachmentError`

---

## Installation

```bash
pip install -r requirements.txt
playwright install firefox    # optional - only for python -m gptpp.login / --browser
```

Credentials are stored under `gptpp/` (gitignored).

---

## Quick Start

### No Login Needed

The anonymous mobile-web endpoint answers plain HTTP — no cookies, no Playwright:

```python
import asyncio
from gptpp import MwebChatClient

async def main():
    async with MwebChatClient() as client:
        print(await client.send_message("Hello!"))          # buffered
        async for chunk in client.send_message_stream("Tell me a story"):
            print(chunk, end="", flush=True)                # streaming

        # conversation continues server-side across calls
        print(await client.send_message("What did I just ask?"))

asyncio.run(main())
```

Or chat interactively (pure HTTP, `new` resets the conversation):

```bash
python -m gptpp.client
```

### Log In (Recommended for /backend-api)

```bash
python -m gptpp.login
```

Opens Playwright Firefox at `https://chatgpt.com/`. Log in normally — the script pulls `accessToken` from `/api/auth/session` (falling back to sniffed `Authorization` headers), grabs cookies, queries `/backend-api/models`, and saves everything to `gptpp/.chatgpt_credentials.json`.

> [!NOTE]
> The browser session is remembered in `gptpp/.chatgpt_browser_state.json`. Reruns restore it and skip login while the token stays valid; `client.py` reuses the same file, so it starts logged in too. Pass `--timeout 900` if you need more time.

### One-Shot

```python
import asyncio
from gptpp import ChatGptApiClient

async def main():
    # credentials if available, otherwise an anonymous client whose
    # completions go out over /unauth-mweb (pure HTTP, no login)
    client = ChatGptApiClient.auto_init() or ChatGptApiClient()

    reply = await client.send_message("Hello!")
    print(reply)

    async for chunk in client.send_message_stream("Tell me a story"):
        print(chunk, end="", flush=True)

    result = await client.send_message_full("Explain relativity")
    print("Thinking:", result["thinking"])

    await client.close()

asyncio.run(main())
```

Transport order per turn: `/backend-api/conversation` (SSE) → `/unauth-mweb/conversation/updates` (declarative HTML) → Playwright, only when `browser_fallback=True`. The first two are plain HTTP.

### Per-Request Options

```python
reply = await client.send_message(
    "Latest AI news?",
    model="auto",                 # see client.models()
    web_search=True,              # force_use_search
    deep_think=True,
    reasoning_effort="low",       # low | medium | high
    attachments=["/path/to/img.png"],
    chat_id="conv_...",           # pin a conversation
    timeout=180,
)
```

`send_messages` / `send_messages_stream` take a full `[{role, content}, ...]` list. `send_message_stream(..., use_history=False)` starts fresh instead of appending to the client's in-memory history.

> [!NOTE]
> `model`, `web_search`, `deep_think`, `reasoning_effort`, and `attachments` live on `/backend-api` only — the anonymous mobile-web replay ignores them and answers with the default model.

---

## Browser Client

```python
import asyncio
from gptpp import ChatGptClient

async def chat():
    async with ChatGptClient(headless=True) as client:
        # optional - logged-out works for a few turns, then the quota dialog
        # appears; log in once and the session is remembered
        # await client.wait_for_auth()
        async for chunk in client.send_message_stream("Hello"):
            print(chunk, end="", flush=True)

asyncio.run(chat())
```

Interactive chat through the browser (streams live, thinking shown inline):

```bash
python -m gptpp.client --browser
```

(`python -m gptpp.client` without `--browser` is the pure-HTTP REPL — see Quick Start.)

The browser client reads the answer two ways, because chatgpt.com has two wire formats:

| Flow | Endpoint | Transport | How we read it |
|---|---|---|---|
| logged-in desktop app | `POST /backend-api/conversation` | SSE (`data:` frames) | in-page `fetch` wrapper (`window.__gpt_sse`) |
| logged-out mobile web | `POST /unauth-mweb/conversation/updates` | declarative-partial-update HTML | DOM polling of `[data-message-role="assistant"]` + `data-conversation-control` markers |
| pure HTTP (no browser) | `POST /unauth-mweb/conversation/updates` | declarative-partial-update HTML | `gptpp/mweb.py` incremental frame parser |

Either way the page's own sentinel/turnstile headers are what actually goes out — which is why this path still works when `/backend-api` rejects plain HTTP. Completion is detected from `message-stream-complete` / `terminal-received` markers (mobile web) or the SSE `finish_details` frame (desktop), with a stall timeout as a last resort.

> [!NOTE]
> The logged-out flow answers in whole committed blocks, so you get one delta per block rather than token-by-token. Log in (`python -m gptpp.login`) for the SSE path.

> [!NOTE]
> Logged-out sessions hit a quota after a few turns: chatgpt.com opens `#mobile-auth-dialog` and blocks sending. The client closes the dialog when it can and raises a clear "run `python -m gptpp.login`" error when it can't.

---

## Project Structure

```
GPT4Free/
│
├── README.md
├── LICENSE
├── requirements.txt
├── .gitignore
│
└── gptpp/
    ├── __init__.py      # public exports (lazy imports)
    ├── api.py           # async pure-HTTP chatgpt.com client (/backend-api)
    ├── mweb.py          # anonymous /unauth-mweb transport + DPU HTML parser
    ├── sentinel.py      # fingerprint blob + FNV-1a proof-of-work + chat-requirements
    ├── client.py        # async Playwright browser client + REPL
    ├── login.py         # capture logged-in bearer token + cookies
    └── errors.py        # AuthError / UpstreamError / SentinelError / ...
```

---

## Architecture

### 1. Sentinel Layer (`sentinel.py`)

`POST /backend-api/sentinel/chat-requirements/prepare` takes a fingerprint blob `gAAAAAC…` (base64 of the frontend's 25-slot `getConfig()` array: screen, UA, jsHeapSizeLimit, nav timing, feature flags…), and `/finalize` returns a PoW challenge. The answer is `gAAAAAB…~S` where `S` is found by FNV-1a + fmix32 hashing until `fnv_hex(seed + b64)[:len(difficulty)] <= difficulty`. Tokens come back as `OpenAI-Sentinel-Chat-Requirements-Token`, `OpenAI-Sentinel-Proof-Token`, `OpenAI-Sentinel-Turnstile-Token`, and are cached until they expire (~9 min).

### 2. API Layer (`api.py`)

Pure-HTTP async client: SSE parsing (`data:` frames, optional `{"v","p"}` envelope, `message_stream_complete`, `parts` = full text so far → delta client-side), conversation pinning (fetch `current_leaf_node`, send `parent_message_id`), attachment upload to `/backend-api/files`, and automatic recovery — 401 → `refresh_session()`, 403 → `refresh_requirements(force=True)`, then the fallback transports below.

### 3. Mobile-Web Layer (`mweb.py`)

The anonymous `chatgpt.com` web app completes through `POST /unauth-mweb/conversation/updates`, which needs no login and no Turnstile. Per turn:

1. warm-up `GET /` for first-party cookies (`__cf_bm`, `oai-did`, `oai-mweb-route` …)
2. `/unauth-mweb/sentinel/chat-requirements/prepare` → local PoW → `/finalize`
3. `/unauth-mweb/conversation/prepare` → `conduit_token` (sent as `x-conduit-token`)
4. the `updates` POST (form-encoded: `prompt`, `chatRequirementsToken`, `proofToken`, `conversationState`, `clientContextualInfo`, `assistantMessageId` …) answered with a stream of `<template data-web-mobile-dpu-frame>` frames

`DpuParser` walks those frames incrementally: nested `<template for=… apply=…>` directives, `data-conversation-control` markers (`conversation-id`, `message-stream-complete`, `complete`, `failed`), and assistant text in `<tag data-assistant-stream-block-index="N">` elements (paragraphs, lists, headings) or `data-writing-block-source` JSON. Text lives in a `-pending` region while streaming and moves into `-committed-tail` at the end, so committed text always wins over pending text. The `data-conversation-state` JSON on `message-stream-complete` (`backendConversationId`, `parentMessageId`, `userMessageCount`) is replayed verbatim on the next turn — that is how `MwebChatClient` keeps the conversation alive.

`ChatGptApiClient` reaches for this layer whenever `/backend-api` cannot answer (`mweb_fallback=True`, the default).

### 4. Browser Layer (`client.py`)

Playwright Firefox restores `.chatgpt_browser_state.json`, types into `#prompt-textarea`, clicks `[data-testid="send-button"]`, then reads the reply from the intercepted SSE stream (logged-in desktop) or the transcript DOM (logged-out mobile web). Each turn renders into its own node, so deltas are tracked per node id — the previous answer is never replayed. Replays through here automatically when `ChatGptApiClient(browser_fallback=True)`, or on demand with `python -m gptpp.client --browser`.

---

## Notes

> [!CAUTION]
> This project is experimental and based on reverse-engineered behavior of chatgpt.com. The API may change at any time, and use may violate OpenAI's terms of service. Use at your own risk.

> [!NOTE]
> `/backend-api/conversation` still answers 403 sentinel/turnstile enforcement from anonymous sessions (verified live). That is expected: `ChatGptApiClient` makes one attempt and then replays over `/unauth-mweb`, which needs neither login nor Turnstile. When you also want Playwright as a last resort, construct `ChatGptApiClient(browser_fallback=True)`.

> [!NOTE]
> The sentinel endpoints answer 401 until chatgpt.com has handed out its first-party cookies, so the client does a warm-up `GET /` before `chat-requirements/prepare` when you have no saved session (`api.py` and `mweb.py` both do this).

> [!NOTE]
> Anonymous `/unauth-mweb` turns can be refused by upstream policy (the stream emits a `failed` control with `data-failure-reason`). `MwebChatClient` surfaces that as `UpstreamError`; log in (`python -m gptpp.login`) to move onto `/backend-api`, or pass `browser_fallback=True` to keep Playwright behind it.

> [!TIP]
> If requests start failing with 401, call `await client.refresh_session()` — it re-reads `/api/auth/session` with your saved cookies. Rerun `python -m gptpp.login` when the token fully expires.

> [!TIP]
> Conversation continuation: a pinned `chat_id` sends only your last message with `parent_message_id` set to the conversation's current leaf node. Without a pin, history is flattened into a single user message (`System instruction:` prefix for `system` turns).

---

## License

This project is licensed under the [MIT License](LICENSE).
