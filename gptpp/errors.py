class GptError(Exception):
    """Base error for the GPT4Free chatgpt.com stack."""


class AuthError(GptError):
    """Missing / expired chatgpt.com session token or cookies."""


class UpstreamError(GptError):
    """chatgpt.com answered with a non-success status or an SSE error frame."""

    def __init__(self, message: str, status: int | None = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


class SentinelError(GptError):
    """Sentinel / Turnstile enforcement rejected a pure-HTTP completion."""


class TurnstileRequiredError(SentinelError):
    """chat-requirements asked for a Cloudflare Turnstile challenge."""


class AttachmentError(GptError):
    """Attachment upload failed."""
