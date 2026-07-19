"""Best-effort refresh of the canonical codex OAuth access token.

Keeps <CODEX_HOME|~/.codex>/auth.json fresh so imp-spawned codex subprocesses
always copy a live access token. Never raises: every failure is reported as a
RefreshOutcome. No token material is ever returned or logged.
"""
from __future__ import annotations

import base64
import json
import httpx
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


_TOKEN_URL = "https://auth.openai.com/oauth/token"
_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
_HTTP_TIMEOUT = 10.0

_DEAD_CODES = frozenset({
    "refresh_token_expired",
    "refresh_token_reused",
    "refresh_token_invalidated",
})


class RefreshTransientError(Exception):
    """Network error / non-200 / malformed 200 — the existing token may still
    be valid, so the caller proceeds to spawn."""


class RefreshInvalidGrant(Exception):
    """The refresh token is dead; the caller drives a reauth_needed pause."""


def _dead_code(body: dict[str, Any]) -> str | None:
    """Read codex's error code: nested `error.code` first, then a top-level
    `error` string, then a top-level `code` string."""
    err = body.get("error")
    if isinstance(err, dict):
        code = err.get("code")
        if isinstance(code, str):
            return code
    if isinstance(err, str):
        return err
    code = body.get("code")
    return code if isinstance(code, str) else None


def _is_dead_token(resp: httpx.Response) -> bool:
    if resp.status_code == 401:
        return True
    try:
        body = resp.json()
    except ValueError:
        return False
    if not isinstance(body, dict):
        return False
    return _dead_code(body) in _DEAD_CODES


def _request_refresh(refresh_token: str) -> dict[str, Any]:
    """The single HTTP seam — tests monkeypatch this. Returns
    {id_token, access_token, refresh_token} (any may be None) on a well-formed
    200; raises RefreshInvalidGrant on a dead token and RefreshTransientError on
    any transport error, non-200, or malformed 200."""
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            resp = client.post(
                _TOKEN_URL,
                json={
                    "client_id": _CLIENT_ID,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                },
            )
    except httpx.HTTPError as exc:
        raise RefreshTransientError(str(exc)) from exc
    if resp.status_code == 200:
        try:
            body = resp.json()
        except ValueError as exc:
            raise RefreshTransientError("malformed token response") from exc
        if not isinstance(body, dict):
            raise RefreshTransientError("malformed token response")
        return {
            "id_token": body.get("id_token"),
            "access_token": body.get("access_token"),
            "refresh_token": body.get("refresh_token"),
        }
    if _is_dead_token(resp):
        raise RefreshInvalidGrant()
    raise RefreshTransientError(f"token endpoint returned {resp.status_code}")
