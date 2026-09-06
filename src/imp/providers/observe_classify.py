# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""Shared agent-text error classifier for the observe runners (claude_code, codex)."""

from __future__ import annotations

import re
import time

from imp.providers import errors as perr
from imp.providers.quota import QuotaReading

# Prose Anthropic emits locally after clearing the saved credentials: terminal,
# unless the same scan carries a routable status code (see _classify_agent_text).
_AUTH_PATTERNS = (
    "Invalid API key",
    "Please run `/login`",
    "Please run /login",
    "Invalid authentication credentials",
    "Failed to authenticate",
)

_QUOTA_PATTERNS = (
    "You have reached your usage limit",
    "Credit balance is too low",
)

# Usage-limit wording backstop: matches both "session limit" and "weekly limit"
# phrasings, plus the bare-time reset announcement. Spans both usage-limit kinds.
_USAGE_LIMIT_RE = re.compile(
    r"hit (?:your|a|the) (?:session|weekly) limit"
    r"|resets\s+\d{1,2}:\d{2}[^\s]*\s*\(America/",
    re.IGNORECASE)

# Matches "API Error: <code>" surfaced by the claude CLI as assistant text when
# the underlying Anthropic API returns a non-2xx. 401/403 → AuthError; 408 and
# 5xx → NetworkError; 429 → RateLimitError; other 4xx → ApiError (Pause);
# previously the 5xx case fell through to None and was treated as agent text →
# wrong nudge loop.
_API_ERROR_RE = re.compile(r"API Error:\s*(\d{3})(?!\d)")

_RESET_MARGIN_SECONDS = 120.0
_MAX_WEEKLY_WAIT_SECONDS = 8 * 24 * 3600.0
# Forgiving: a codex reading is taken from the previous turn's rollout record.
_HINT_FLOOR_PCT = 90.0


def _hint_from_reading(reading: QuotaReading | None, now: float) -> float | None:
    """Seconds until the earliest reset among saturated windows, + a 2-minute
    margin, or None. Earliest, not highest: over-waiting idles the quest for up
    to a week, under-waiting costs one retry that carries a fresher reading."""
    if reading is None:
        return None
    resets = [w.resets_at for w in reading.windows
              if w.used_pct >= _HINT_FLOOR_PCT and w.resets_at is not None]
    if not resets:
        return None
    delta = min(resets) - now
    if delta <= 0 or delta > _MAX_WEEKLY_WAIT_SECONDS:
        return None
    return delta + _RESET_MARGIN_SECONDS


def _rate_limit_with_reset_hint(
    text: str, reading: QuotaReading | None,
) -> perr.RateLimitError:
    """Build the usage-limit RateLimitError, attaching the derived reset time as
    detail={"retry_after_seconds": <hint>} when one is available. The key is
    disjoint from every diagnostic key _termination_from_result later merges via
    labeled.detail.update(...), so a hint set here survives that merge."""
    hint = _hint_from_reading(reading, time.time())
    detail: dict[str, object] | None = (
        {"retry_after_seconds": hint} if hint is not None else None
    )
    return perr.RateLimitError(f"claude CLI: {text[:200]}", detail=detail)


def _classify_agent_text(
    text: str, *, anchored: bool = True, reading: QuotaReading | None = None,
) -> perr.ProviderError | None:
    """Detect known claude CLI terminal-error patterns in agent text.

    The CLI catches its own errors and returns them as a text response
    with exit 0 (no out-of-band signal). Exact-substring patterns,
    conservatively chosen: false negatives fall back to existing
    retry behavior; false positives would convert recoverable agent
    confusion into fatal pause.

    When `anchored` (the default), patterns must match within the first line
    of the text: every known genuine CLI error is a terse message that leads
    with the error, while an agent that merely *quotes* a pattern buries it
    mid-document behind a prose opener (the redu99 false positive — a
    subagent report about _AUTH_PATTERNS was reclassified as AuthTerminalError, its
    successful turn rolled back, and the retry returned an empty stub).
    Callers that already hold an out-of-band error signal
    (_termination_from_result) pass anchored=False: there the substring only
    picks the error category, and a miss degrades to TerminatedError, not a
    false pause.

    Auth prose is terminal only without server evidence: Anthropic emits those
    patterns locally after clearing the saved credentials, without sending a
    request, so no retry can heal them. A routable "API Error: <code>" in the
    same scan is proof a request reached the server and came back, so the prose
    is a server rejection and routes on the code instead. A codeless
    "API Error:" is a transport failure — the request never arrived — so it
    cannot overturn the terminal reading, and neither can a matched-but-unrouted
    code (a 3xx), which no branch below would route.

    Quota patterns keep their precedence and win over any code. Routing:
    401/403 → AuthError with kind="http_<code>"; 408 → NetworkError (transient
    timeout); 429 → RateLimitError; other 4xx → ApiError with kind="http_<code>";
    5xx → NetworkError (network category → RetryWithBackoff; §10); statusless
    "API Error:" → NetworkError.
    """
    scan = text.lstrip().split("\n", 1)[0] if anchored else text
    m = _API_ERROR_RE.search(scan)
    code = int(m.group(1)) if m else None
    # 400-599 is exactly the set the branches below route; an unrouted code
    # would otherwise drop the text past every branch and return None.
    routable = code is not None and 400 <= code < 600
    if not routable:
        for pat in _AUTH_PATTERNS:
            if pat in scan:
                return perr.AuthTerminalError(f"claude CLI: {text[:200]}")
    for pat in _QUOTA_PATTERNS:
        if pat in scan:
            return perr.QuotaError(f"claude CLI: {pat}")
    if _USAGE_LIMIT_RE.search(scan):
        return _rate_limit_with_reset_hint(text, reading)
    if code is not None:
        if code == 429:
            return _rate_limit_with_reset_hint(text, reading)
        if code in (401, 403):
            auth_exc = perr.AuthError(f"claude CLI: {text[:200]}")
            auth_exc.kind = f"http_{code}"  # type: ignore[attr-defined]
            return auth_exc
        if code == 408:
            return perr.NetworkError(f"claude CLI: {text[:200]}")
        if 400 <= code < 500:
            exc = perr.ApiError(f"claude CLI: {text[:200]}")
            # Carried through to make_error_event(kind=...) at the call sites so
            # the event records kind="http_400" rather than the class name.
            exc.kind = f"http_{code}"  # type: ignore[attr-defined]
            return exc
        if 500 <= code < 600:
            # §10: 5xx (529 Overloaded et al.) is transient — route to the
            # `network` category, which maps to RetryWithBackoff (same routing the
            # SDK providers use for an InternalServerError).
            return perr.NetworkError(f"claude CLI: {text[:200]}")
    elif "API Error:" in scan:
        # A statusless `API Error:` — no routable HTTP code — means the request
        # never reached the server (ConnectionRefused, reset, EOF, transport
        # timeout). Broad transport stance: transient -> retry, not pause. A
        # 3xx/unrouted code that matched the regex above is NOT statusless, so
        # it falls through to None (Pause) here.
        return perr.NetworkError(f"claude CLI: {text[:200]}")
    return None


def classifies_as_provider_error(text: str) -> bool:
    """Anchored classification as a predicate: is this prose provider chrome?"""
    return _classify_agent_text(text) is not None
