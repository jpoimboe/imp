"""Best-effort refresh of the canonical codex OAuth access token.

Keeps <CODEX_HOME|~/.codex>/auth.json fresh so imp-spawned codex subprocesses
always copy a live access token. Never raises: every failure is reported as a
RefreshOutcome. No token material is ever returned or logged.
"""
from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

_BUFFER_MS = 5 * 60 * 1000
_REFRESH_INTERVAL_DAYS = 8
_DAY_MS = 24 * 60 * 60 * 1000


def _jwt_exp_ms(access_token: object) -> int | None:
    """Return the JWT `exp` claim in ms (no signature verification), or None
    when `access_token` is not a JWT / has no numeric `exp`."""
    if not isinstance(access_token, str) or not access_token:
        return None
    parts = access_token.split(".")
    if len(parts) < 2:
        return None
    seg = parts[1]
    padding = "=" * (-len(seg) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(seg + padding))
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    exp = payload.get("exp")
    if isinstance(exp, bool) or not isinstance(exp, (int, float)):
        return None
    return int(exp) * 1000


def _parse_last_refresh_ms(last_refresh: object) -> int | None:
    """Parse an RFC3339 UTC `last_refresh` string to ms, or None when absent
    /unparseable. A naive timestamp is treated as UTC."""
    if not isinstance(last_refresh, str) or not last_refresh:
        return None
    try:
        dt = datetime.fromisoformat(last_refresh.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _needs_refresh(
    tokens: Mapping[str, Any], last_refresh: object, now_ms: int,
) -> bool:
    """Mirror codex's should_refresh_proactively: prefer the access-token JWT
    `exp` (refresh within a 5-min buffer); else the 8-day `last_refresh`
    fallback; else refresh (fail toward refreshing under the lock's re-check)."""
    exp_ms = _jwt_exp_ms(tokens.get("access_token"))
    if exp_ms is not None:
        return exp_ms <= now_ms + _BUFFER_MS
    lr_ms = _parse_last_refresh_ms(last_refresh)
    if lr_ms is not None:
        return lr_ms <= now_ms - _REFRESH_INTERVAL_DAYS * _DAY_MS
    return True
