"""Shared agent-text error classifier for the observe runners (claude_code, codex)."""

from __future__ import annotations

import re
import time
import zoneinfo
from datetime import datetime, timedelta

from imp.providers import errors as perr

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

# Matches the CLI usage-limit reset announcement in two shapes:
#   bare-time (session):  "resets 4am (America/Los_Angeles)", "resets 16:00 (...)"
#   dated (weekly):       "resets Jul 16, 8pm (America/Los_Angeles)"
# The optional leading "<Mon> <DD>," group (present only on the weekly form)
# selects the dated branch in _reset_hint_seconds.
_RESET_RE = re.compile(
    r"resets\s+(?:([A-Za-z]{3})\s+(\d{1,2}),\s+)?"
    r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*\(([^)]+)\)",
    re.IGNORECASE,
)
_MONTH_ABBR = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_RESET_MARGIN_SECONDS = 120.0
_MAX_RESET_WAIT_SECONDS = 6 * 3600.0
_MAX_WEEKLY_WAIT_SECONDS = 8 * 24 * 3600.0


def _reset_hint_seconds(text: str, now: float) -> float | None:
    """Seconds to wait until the announced usage-limit reset, + a 2-minute
    margin, or None. `now` is epoch seconds, injected for testability.

    Two shapes, keyed off whether the match carries a date:
      - Bare-time (session): today-or-tomorrow, distrusted beyond 6 h
        (session windows are 5 h, so a larger figure means a bad parse).
      - Dated (weekly): the explicit month/day + time in the stated zone,
        this year and rolled forward a year if already past; distrusted
        beyond 8 days.
    Returns None on any parse failure, an unknown month or zone, an
    out-of-range time, a still-past target, or an over-ceiling wait.
    None means exactly today's fall-back backoff behavior.
    """
    m = _RESET_RE.search(text)
    if m is None:
        return None
    month_s, day_s, hour_s, minute_s, ampm, zone_name = m.groups()
    try:
        tz = zoneinfo.ZoneInfo(zone_name)
    except (KeyError, ValueError):
        # ZoneInfoNotFoundError subclasses KeyError; a malformed name -> ValueError.
        return None
    hour = int(hour_s)
    minute = int(minute_s) if minute_s else 0
    if ampm:
        if not 1 <= hour <= 12:
            return None
        if ampm.lower() == "pm" and hour != 12:
            hour += 12
        elif ampm.lower() == "am" and hour == 12:
            hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    now_local = datetime.fromtimestamp(now, tz=tz)
    if month_s is not None:
        month = _MONTH_ABBR.get(month_s.lower())
        if month is None:
            return None
        try:
            target = now_local.replace(
                month=month, day=int(day_s), hour=hour, minute=minute,
                second=0, microsecond=0)
            if target <= now_local:
                target = target.replace(year=target.year + 1)
        except ValueError:
            # An impossible day for the month (e.g. "Feb 30") or a Feb-29
            # roll into a non-leap year.
            return None
        if target <= now_local:
            return None
        ceiling = _MAX_WEEKLY_WAIT_SECONDS
    else:
        target = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now_local:
            target += timedelta(days=1)
        ceiling = _MAX_RESET_WAIT_SECONDS
    delta = target.timestamp() - now
    if delta > ceiling:
        return None
    return delta + _RESET_MARGIN_SECONDS


def _rate_limit_with_reset_hint(text: str) -> perr.RateLimitError:
    """Build the usage-limit RateLimitError, attaching the parsed reset time as
    detail={"retry_after_seconds": <hint>} when one is available. The key is
    disjoint from every diagnostic key _termination_from_result later merges via
    labeled.detail.update(...), so a hint set here survives that merge."""
    hint = _reset_hint_seconds(text, time.time())
    detail: dict[str, object] | None = (
        {"retry_after_seconds": hint} if hint is not None else None
    )
    return perr.RateLimitError(f"claude CLI: {text[:200]}", detail=detail)


def _classify_agent_text(
    text: str, *, anchored: bool = True,
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
    subagent report about _AUTH_PATTERNS was reclassified as AuthError, its
    successful turn rolled back, and the retry returned an empty stub).
    Callers that already hold an out-of-band error signal
    (_termination_from_result) pass anchored=False: there the substring only
    picks the error category, and a miss degrades to TerminatedError, not a
    false pause.

    Auth/quota patterns are checked first so they win when both substrings
    appear. The API-error regex matches "API Error: <code>":
    401/403 → AuthError with kind="http_<code>"; 408 → NetworkError (transient
    timeout); 429 → RateLimitError; other 4xx → ApiError with kind="http_<code>";
    5xx → NetworkError (network category → RetryWithBackoff; §10).
    """
    scan = text.lstrip().split("\n", 1)[0] if anchored else text
    for pat in _AUTH_PATTERNS:
        if pat in scan:
            return perr.AuthError(f"claude CLI: {pat}")
    for pat in _QUOTA_PATTERNS:
        if pat in scan:
            return perr.QuotaError(f"claude CLI: {pat}")
    if _USAGE_LIMIT_RE.search(scan):
        return _rate_limit_with_reset_hint(text)
    m = _API_ERROR_RE.search(scan)
    if m:
        code = int(m.group(1))
        if code == 429:
            return _rate_limit_with_reset_hint(text)
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
