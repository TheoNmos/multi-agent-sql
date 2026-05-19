"""LLM request timeouts and user-facing error messages."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable

from app.config import settings

MODEL_TIMEOUT_USER_MESSAGE = (
    "The model provider did not respond within 50 seconds. "
    "This is usually a temporary issue with the model API, please try again."
)


class ModelResponseTimeoutError(TimeoutError):
    """Raised when an LLM request exceeds the configured timeout."""

    def __init__(self, context: str | None = None) -> None:
        self.context = context
        detail = f" ({context})" if context else ""
        super().__init__(f"Model request timed out after {settings.llm_request_timeout_s}s{detail}")


def is_model_timeout(exc: BaseException) -> bool:
    """Return True if the exception chain indicates an LLM timeout."""
    if isinstance(exc, (ModelResponseTimeoutError, asyncio.TimeoutError)):
        return True

    message = str(exc).lower()
    timeout_markers = (
        "timed out",
        "timeout",
        "time out",
        "deadline exceeded",
        "read timeout",
        "connect timeout",
    )
    if any(marker in message for marker in timeout_markers):
        return True

    cause = exc.__cause__
    if cause is not None and cause is not exc:
        return is_model_timeout(cause)

    context = exc.__context__
    if context is not None and context is not exc and context is not cause:
        return is_model_timeout(context)

    return False


def format_model_error(exc: BaseException, *, step: str | None = None) -> str:
    """Map provider/timeout failures to a user-friendly message."""
    if is_model_timeout(exc):
        return MODEL_TIMEOUT_USER_MESSAGE

    if step:
        return f"{step} failed: {exc}"
    return str(exc)


async def run_with_llm_timeout[T](
    coro: Awaitable[T],
    *,
    context: str,
    timeout_s: float | None = None,
) -> T:
    """Run an LLM-backed coroutine with a hard asyncio timeout (safety net)."""
    limit = timeout_s if timeout_s is not None else settings.llm_request_timeout_s
    try:
        return await asyncio.wait_for(coro, timeout=limit)
    except TimeoutError as exc:
        raise ModelResponseTimeoutError(context) from exc
