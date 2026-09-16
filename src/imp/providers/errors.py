# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
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

import os
import traceback
from typing import Literal

Category = Literal[
    "auth",
    "auth_terminal",
    "config",
    "quota",
    "content_filter",
    "context_window",
    "rate_limit",
    "network",
    "stalled",
    "api",
    "loop",
    "agent_output_missing",
    "terminated",
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


class AuthTerminalError(ProviderError):
    """A login the provider CLI already gave up on — it cleared the credentials, so
    no retry can heal it. Deliberately NOT an AuthError subclass; maps to Pause."""
    category: Category = "auth_terminal"


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


class ApiError(ProviderError):
    """A hard API error (e.g. claude CLI surfacing 'API Error: 400 ...' as
    plain assistant text). Distinct from `config` so the text-surfaced case is
    recognized at all; maps to Pause."""
    category: Category = "api"


class LoopError(ProviderError):
    """The loop_guard detected a repeated-failure or no-progress loop within a
    single agent invocation. Maps to Pause."""
    category: Category = "loop"


class TerminatedError(ProviderError):
    """Agent terminated with an error whose specific cause could not be typed
    (no recognized quota/auth signal). Maps to Pause."""
    category: Category = "terminated"


def make_error_event(exc: ProviderError, *, kind: str = "") -> dict[str, object]:
    """Build the canonical error event for a classified provider exception.

    ``kind`` is informational (surfaced on the paused line via Fix A;
    preserved for legacy descriptors like ``"transcript_invalid"``). When
    omitted, defaults to the exception class name.
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


def make_unknown_error_event(
    exc: BaseException, detail: dict[str, object] | None = None,
) -> dict[str, object]:
    """Build the canonical 'unknown' error event for the runner's
    top-level fallback ``except Exception`` branch. Single source of
    truth so the wire format stays consistent. ``detail`` lets the runner
    attach captured diagnostics (e.g. the subprocess ``stderr_tail``); a
    raised exception also contributes a ``traceback_tail``, and a
    messageless one names its crash site."""
    out: dict[str, object] = dict(detail or {})
    summary = str(exc)
    tb = exc.__traceback__
    if tb is None:
        summary = summary or type(exc).__name__
    else:
        out["traceback_tail"] = "\n".join(
            "".join(traceback.format_exception(exc)).splitlines()[-15:])
        if not summary:
            frame = traceback.extract_tb(tb)[-1]
            summary = (
                f"{type(exc).__name__} at"
                f" {os.path.basename(frame.filename)}:{frame.lineno}"
            )
    return {
        "type": "error",
        "payload": {
            "category": "unknown",
            "kind": type(exc).__name__,
            "message": f"unclassified provider error: {summary}",
            "detail": out,
        },
    }
