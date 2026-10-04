"""
gptpp - async chatgpt.com chat stack (no OpenAI key, no proxy).

    from gptpp import ChatGptApiClient

    client = ChatGptApiClient.auto_init()          # .chatgpt_credentials.json
    async for chunk in client.send_message_stream("hello"):
        print(chunk, end="", flush=True)

or talk straight to the anonymous mobile-web endpoint (plain HTTP, no login):

    from gptpp import MwebChatClient

    async with MwebChatClient() as client:
        print(await client.send_message("hello"))

Dependencies: aiohttp (HTTP core), playwright (login + browser fallback).
Run `python -m gptpp.login` once to capture a logged-in session; run
`python -m gptpp.client` for the REPL (pure HTTP by default).
"""

from .errors import (
    AttachmentError,
    AuthError,
    GptError,
    SentinelError,
    TurnstileRequiredError,
    UpstreamError,
)

__version__ = "0.1.0"

_LAZY = {
    "ChatGptApiClient": ("gptpp.api", "ChatGptApiClient"),
    "MwebChatClient": ("gptpp.mweb", "MwebChatClient"),
    "DpuParser": ("gptpp.mweb", "DpuParser"),
    "flatten_messages": ("gptpp.mweb", "flatten_messages"),
    "ChatMessage": ("gptpp.client", "ChatMessage"),
    "ChatGptClient": ("gptpp.client", "ChatGptClient"),
    "ChatRequirements": ("gptpp.sentinel", "ChatRequirements"),
    "BrowserProfile": ("gptpp.sentinel", "BrowserProfile"),
    "get_chat_requirements": ("gptpp.sentinel", "get_chat_requirements"),
    "build_requirements_blob": ("gptpp.sentinel", "build_requirements_blob"),
    "solve_pow": ("gptpp.sentinel", "solve_pow"),
    "fnv_hex": ("gptpp.sentinel", "fnv_hex"),
    "login": ("gptpp.login", "login"),
}

__all__ = [
    "GptError",
    "AuthError",
    "UpstreamError",
    "SentinelError",
    "TurnstileRequiredError",
    "AttachmentError",
    "__version__",
    *_LAZY,
]


def __getattr__(name: str):
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    module = importlib.import_module(target[0])
    value = getattr(module, target[1])
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(__all__))
