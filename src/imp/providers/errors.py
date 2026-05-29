"""Typed exception hierarchy + error-event helpers (taxonomy single source).

Providers raise the appropriate `ProviderError` subclass when they can
recognize the error condition. A top-level catch in each provider's
streaming loop translates these into canonical
``{type: error, payload: {category, kind, message, detail}}`` events that
the executor's error-policy can act on.

Do NOT use this hierarchy for tool-side errors — those use
``imp.agents.tools.errors.FatalToolError``.
"""

from __future__ import annotations

from typing import Literal

Category = Literal[
    "auth",
    "config",
    "quota",
    "content_filter",
    "context_window",
    "rate_limit",
    "network",
    "agent_output_missing",
    "agent_incomplete",
    "unknown",
]


class ProviderError(Exception):
    """Base for provider-side errors classified by category."""

    category: Category  # overridden by each subclass

    def __init__(self, message: str, *, detail: dict[str, object] | None = None) -> None:
        super().__init__(message)
        self.detail: dict[str, object] = detail or {}


class AuthError(ProviderError):
    category: Category = "auth"


class ConfigError(ProviderError):
    category: Category = "config"


class QuotaError(ProviderError):
    category: Category = "quota"


class ContentFilterError(ProviderError):
    category: Category = "content_filter"


class ContextWindowError(ProviderError):
    category: Category = "context_window"


class RateLimitError(ProviderError):
    category: Category = "rate_limit"


class NetworkError(ProviderError):
    category: Category = "network"


def make_error_event(exc: ProviderError, *, kind: str = "") -> dict[str, object]:
    """Build the canonical error event for a classified provider exception.

    ``kind`` is informational (rendered nowhere today; preserved for
    legacy descriptors like ``"transcript_invalid"``). When omitted,
    defaults to the exception class name.
    """
    return {
        "type": "error",
        "payload": {
            "category": exc.category,
            "kind": kind or type(exc).__name__,
            "message": str(exc),
            "detail": exc.detail,
        },
    }


def make_unknown_error_event(exc: BaseException) -> dict[str, object]:
    """Build the canonical 'unknown' error event for the runner's
    top-level fallback ``except Exception`` branch. Single source of
    truth so the wire format stays consistent.
    """
    return {
        "type": "error",
        "payload": {
            "category": "unknown",
            "kind": type(exc).__name__,
            "message": f"unclassified provider error: {exc}",
            "detail": {},
        },
    }
