"""
Async HTTP transport for gptpp backed by browser-impersonating TLS.

chatgpt.com's Cloudflare challenges plain Python clients: aiohttp's
TLS/HTTP2 fingerprint reads as a bot on many host IPs (the server gets
a JS challenge, Cloudflare Workers get a hard 403). curl_cffi speaks a
real browser's fingerprint - TLS, HTTP/2 settings and headers from the
same profile - and is answered like that browser everywhere, so every
request in this package goes through it and no relay is needed.

The surface mirrors the aiohttp calls this package used before:

    session = Session(headers={"User-Agent": ...})
    async with session.get(url, timeout=30) as resp:
        resp.status, resp.headers
        await resp.text() / await resp.read()
        async for chunk in resp.iter_any()      # streamed bodies
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Mapping, Optional

from curl_cffi.requests import AsyncSession
from curl_cffi.requests.exceptions import RequestException

# aiohttp's ClientError equivalent - transport-level failures
ClientError = RequestException

# browser profile behind every request; keeps TLS, HTTP/2 and header
# defaults internally consistent (request-level headers still override)
IMPERSONATE = "firefox133"


def _total(timeout: Any) -> float:
    """Seconds to wait - tolerates plain floats and aiohttp.ClientTimeout."""
    if timeout is None:
        return 60.0
    total = getattr(timeout, "total", None)
    return float(total if total is not None else timeout)


class Response:
    """Response facade: async context manager, buffered or streamed."""

    def __init__(self, raw: Any):
        self._raw = raw
        self.status = raw.status_code
        self.headers = raw.headers

    async def __aenter__(self) -> "Response":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    @property
    def _streaming(self) -> bool:
        return bool(getattr(self._raw, "queue", None))

    async def text(self) -> str:
        if self._streaming:
            return await self._raw.atext()
        return self._raw.text

    async def read(self) -> bytes:
        if self._streaming:
            return await self._raw.acontent()
        return self._raw.content

    def iter_any(self) -> AsyncIterator[bytes]:
        """Yield raw body chunks as they arrive (streamed responses)."""

        async def gen() -> AsyncIterator[bytes]:
            async for chunk in self._raw.aiter_content():
                if chunk:
                    yield chunk

        return gen()

    async def close(self) -> None:
        if self._streaming:
            await self._raw.aclose()


class _RequestCM:
    """Awaitable AND async-CM request, like aiohttp's _RequestContextManager."""

    def __init__(self, coro: Any):
        self._coro = coro
        self._resp: Optional[Response] = None

    def __await__(self) -> Any:
        return self._coro.__await__()

    async def __aenter__(self) -> Response:
        self._resp = await self._coro
        return self._resp

    async def __aexit__(self, *exc: Any) -> None:
        if self._resp is not None:
            await self._resp.close()


class Session:
    """Cookie-keeping session with a fixed browser fingerprint."""

    def __init__(
        self,
        headers: Optional[Mapping[str, str]] = None,
        impersonate: str = IMPERSONATE,
        loop: Any = None,
        **kwargs: Any,
    ):
        if loop is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
        # callers compare this against the running loop (was aiohttp._loop)
        self._loop = loop
        self._session = AsyncSession(
            headers=dict(headers or {}),
            impersonate=impersonate,
            **kwargs,
        )

    async def __aenter__(self) -> "Session":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    @property
    def cookies(self) -> Any:
        return self._session.cookies

    @property
    def closed(self) -> bool:
        return bool(getattr(self._session, "_closed", False))

    def get(
        self,
        url: str,
        *,
        headers: Optional[Mapping[str, str]] = None,
        timeout: Any = 60,
        **kwargs: Any,
    ) -> _RequestCM:
        return self._request("GET", url, headers=headers, timeout=timeout, **kwargs)

    def post(
        self,
        url: str,
        *,
        headers: Optional[Mapping[str, str]] = None,
        data: Any = None,
        json: Any = None,
        timeout: Any = 60,
        **kwargs: Any,
    ) -> _RequestCM:
        return self._request(
            "POST",
            url,
            headers=headers,
            data=data,
            json=json,
            timeout=timeout,
            **kwargs,
        )

    def put(
        self,
        url: str,
        *,
        headers: Optional[Mapping[str, str]] = None,
        data: Any = None,
        timeout: Any = 60,
        **kwargs: Any,
    ) -> _RequestCM:
        return self._request("PUT", url, headers=headers, data=data, timeout=timeout, **kwargs)

    def _request(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[Mapping[str, str]] = None,
        timeout: Any = 60,
        stream: bool = False,
        **kwargs: Any,
    ) -> _RequestCM:
        async def go() -> Response:
            raw = await self._session.request(
                method,
                url,
                headers=headers,
                timeout=_total(timeout),
                stream=stream,
                **kwargs,
            )
            return Response(raw)

        return _RequestCM(go())

    async def close(self) -> None:
        await self._session.close()
