# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""Best-effort refresh of the canonical codex OAuth access token.

Keeps <CODEX_HOME|~/.codex>/auth.json fresh so imp-spawned codex subprocesses
always copy a live access token. Never raises: every failure is reported
structurally in the return value. No token material is ever returned or logged.
"""
from __future__ import annotations

import base64
import contextlib
import json
import math
import os
import stat
import tempfile
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import filelock
import httpx

from imp.providers.paths import codex_canonical_dir

_BUFFER_MS = 5 * 60 * 1000
_REFRESH_INTERVAL_DAYS = 8
_DAY_MS = 24 * 60 * 60 * 1000

REFRESHED = "refreshed"
SKIPPED_FRESH = "skipped_fresh"
SKIPPED_ROTATED = "skipped_rotated"
NOT_APPLICABLE = "not_applicable"
FAILED_TRANSIENT = "failed_transient"
FAILED_INVALID_GRANT = "failed_invalid_grant"

_LOCK_FILE = ".imp-codex-token-refresh.lock"
_LOCK_TIMEOUT = 30.0


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
    if isinstance(exp, float) and not math.isfinite(exp):
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
        dt = dt.replace(tzinfo=UTC)
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


class LockTimeout(Exception):
    """The refresh lock could not be acquired within its timeout."""


@dataclass(frozen=True)
class RefreshOutcome:
    outcome: str
    expires_at: int | None = None


def _load_auth(auth_path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(auth_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _is_apikey(data: dict[str, Any], env: Mapping[str, str]) -> bool:
    """`detect_auth_mode`'s precedence: an explicit auth_mode wins both ways."""
    mode: object = data.get("auth_mode")
    if mode in ("chatgpt", "apikey"):
        return mode == "apikey"
    return bool(data.get("OPENAI_API_KEY") or env.get("OPENAI_API_KEY"))


def _refresh_token_of(tokens: Mapping[str, Any]) -> str | None:
    rt = tokens.get("refresh_token")
    return rt if isinstance(rt, str) and rt else None


def _rfc3339_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


@contextlib.contextmanager
def _token_lock(lock_path: Path, *, timeout: float) -> Iterator[None]:
    fl = filelock.FileLock(str(lock_path))
    try:
        fl.acquire(timeout=timeout)
    except filelock.Timeout as exc:
        raise LockTimeout() from exc
    try:
        yield
    finally:
        fl.release()


def _atomic_write_auth(auth_path: Path, data: dict[str, Any]) -> None:
    payload = json.dumps(data, indent=2).encode("utf-8")
    fd, tmp_name = tempfile.mkstemp(dir=auth_path.parent, prefix=".auth-")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(tmp, auth_path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def _applicable_tokens(
    data: dict[str, Any] | None, env: Mapping[str, str],
) -> tuple[dict[str, Any], str] | None:
    """Return (tokens, refresh_token) when `data` is a live chatgpt-mode bundle
    with a usable refresh token, else None."""
    if data is None or _is_apikey(data, env):
        return None
    tokens = data.get("tokens")
    if not isinstance(tokens, dict):
        return None
    refresh_token = _refresh_token_of(tokens)
    if refresh_token is None:
        return None
    return tokens, refresh_token


def refresh_canonical_token(
    now_ms: int, *, env: Mapping[str, str] | None = None, force: bool = False,
    spawn_expires_at: int | None = None,
) -> RefreshOutcome:
    """`force` ignores expiry; `spawn_expires_at` still skips a peer's rotation."""
    env = os.environ if env is None else env
    try:
        base = codex_canonical_dir(env)
    except RuntimeError:
        return RefreshOutcome(NOT_APPLICABLE)
    auth_path = base / "auth.json"

    data = _load_auth(auth_path)
    applicable = _applicable_tokens(data, env)
    if data is None or applicable is None:
        return RefreshOutcome(NOT_APPLICABLE)
    tokens, _ = applicable
    if not force and not _needs_refresh(tokens, data.get("last_refresh"), now_ms):
        return RefreshOutcome(SKIPPED_FRESH)

    try:
        with _token_lock(base / _LOCK_FILE, timeout=_LOCK_TIMEOUT):
            data = _load_auth(auth_path)
            applicable = _applicable_tokens(data, env)
            if data is None or applicable is None:
                return RefreshOutcome(NOT_APPLICABLE)
            tokens, refresh_token = applicable
            if not force and not _needs_refresh(tokens, data.get("last_refresh"), now_ms):
                return RefreshOutcome(SKIPPED_FRESH)
            current_expires_at = _jwt_exp_ms(tokens.get("access_token"))
            if (spawn_expires_at is not None and current_expires_at is not None
                    and current_expires_at != spawn_expires_at):
                return RefreshOutcome(SKIPPED_ROTATED)
            try:
                new_tokens = _request_refresh(refresh_token)
            except RefreshInvalidGrant:
                return RefreshOutcome(FAILED_INVALID_GRANT)
            except RefreshTransientError:
                return RefreshOutcome(FAILED_TRANSIENT)
            merged_tokens = dict(tokens)
            for key in ("id_token", "access_token", "refresh_token"):
                if new_tokens.get(key) is not None:
                    merged_tokens[key] = new_tokens[key]
            merged = dict(data)
            merged["tokens"] = merged_tokens
            merged["last_refresh"] = _rfc3339_now()
            _atomic_write_auth(auth_path, merged)
            return RefreshOutcome(
                REFRESHED, _jwt_exp_ms(merged_tokens.get("access_token")))
    except LockTimeout:
        return RefreshOutcome(FAILED_TRANSIENT)
    except OSError:
        return RefreshOutcome(FAILED_TRANSIENT)


def canonical_expires_at(*, env: Mapping[str, str] | None = None) -> int | None:
    """The canonical access token's expiry — the value a rotation race moves."""
    env = os.environ if env is None else env
    try:
        base = codex_canonical_dir(env)
    except RuntimeError:
        return None
    data = _load_auth(base / "auth.json")
    if data is None:
        return None
    tokens = data.get("tokens")
    if not isinstance(tokens, dict):
        return None
    return _jwt_exp_ms(tokens.get("access_token"))
