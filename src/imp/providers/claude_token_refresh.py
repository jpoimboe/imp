"""Best-effort refresh of the canonical Claude OAuth access token.

Keeps <canonical>/.credentials.json fresh so imp-spawned (redacted) claude
subprocesses always copy a live access token. Never raises: every failure is
reported as a RefreshOutcome. No token material is ever returned or logged.
"""
from __future__ import annotations

import contextlib
import json
import os
import stat
import tempfile
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import filelock
import httpx

REFRESHED = "refreshed"
SKIPPED_FRESH = "skipped_fresh"
NOT_APPLICABLE = "not_applicable"
FAILED_TRANSIENT = "failed_transient"
FAILED_INVALID_GRANT = "failed_invalid_grant"

_BUFFER_MS = 5 * 60 * 1000
_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
_LOCK_FILE = ".imp-token-refresh.lock"
_LOCK_TIMEOUT = 30.0
_HTTP_TIMEOUT = 10.0


class RefreshTransientError(Exception):
    """Network error / 5xx / timeout — retry rides on the spawn's 401 backoff."""


class RefreshInvalidGrant(Exception):
    """The refresh token is dead; the shipped reauth_needed pause handles it."""


class LockTimeout(Exception):
    """The refresh lock could not be acquired within its timeout."""


@dataclass(frozen=True)
class RefreshOutcome:
    outcome: str
    expires_at: int | None = None


def _canonical_dir(env: Mapping[str, str]) -> Path:
    override = env.get("CLAUDE_CONFIG_DIR")
    return Path(override) if override else Path.home() / ".claude"


def _load_credentials(creds_path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(creds_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _oauth_of(data: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(data, dict):
        return None
    oauth = data.get("claudeAiOauth")
    return oauth if isinstance(oauth, dict) else None


def _refresh_token_of(oauth: dict[str, Any]) -> str | None:
    rt = oauth.get("refreshToken")
    return rt if isinstance(rt, str) and rt else None


def _needs_refresh(oauth: dict[str, Any], now_ms: int) -> bool:
    v = oauth.get("expiresAt")
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return True
    return int(v) <= now_ms + _BUFFER_MS


def _request_refresh(refresh_token: str) -> dict[str, Any]:
    """The single HTTP seam — tests monkeypatch this. Returns a normalized token
    dict on a well-formed 200; raises RefreshInvalidGrant on a dead token and
    RefreshTransientError on any transport error, non-200, or malformed 200."""
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            resp = client.post(
                _TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": _CLIENT_ID,
                },
            )
    except httpx.HTTPError as exc:
        raise RefreshTransientError(str(exc)) from exc
    if resp.status_code == 200:
        try:
            body = resp.json()
            return {
                "access_token": body["access_token"],
                "refresh_token": body["refresh_token"],
                "expires_in": int(body["expires_in"]),
            }
        except (ValueError, KeyError, TypeError) as exc:
            raise RefreshTransientError("malformed token response") from exc
    if resp.status_code == 400 and _is_invalid_grant(resp):
        raise RefreshInvalidGrant()
    raise RefreshTransientError(f"token endpoint returned {resp.status_code}")


def _is_invalid_grant(resp: httpx.Response) -> bool:
    try:
        body = resp.json()
    except ValueError:
        return False
    return isinstance(body, dict) and body.get("error") == "invalid_grant"


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


def _atomic_write_creds(creds_path: Path, data: dict[str, Any]) -> None:
    payload = json.dumps(data, indent=2).encode("utf-8")
    fd, tmp_name = tempfile.mkstemp(dir=creds_path.parent, prefix=".credentials-")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(tmp, creds_path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def refresh_canonical_token(
    now_ms: int, *, env: Mapping[str, str] | None = None,
) -> RefreshOutcome:
    env = os.environ if env is None else env
    if env.get("ANTHROPIC_API_KEY") or env.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return RefreshOutcome(NOT_APPLICABLE)
    base = _canonical_dir(env)
    creds_path = base / ".credentials.json"
    oauth = _oauth_of(_load_credentials(creds_path))
    if oauth is None or _refresh_token_of(oauth) is None:
        return RefreshOutcome(NOT_APPLICABLE)
    if not _needs_refresh(oauth, now_ms):
        return RefreshOutcome(SKIPPED_FRESH)
    try:
        with _token_lock(base / _LOCK_FILE, timeout=_LOCK_TIMEOUT):
            data = _load_credentials(creds_path)
            oauth = _oauth_of(data)
            refresh_token = None if oauth is None else _refresh_token_of(oauth)
            if data is None or oauth is None or refresh_token is None:
                return RefreshOutcome(NOT_APPLICABLE)
            if not _needs_refresh(oauth, now_ms):
                return RefreshOutcome(SKIPPED_FRESH)
            try:
                tokens = _request_refresh(refresh_token)
            except RefreshInvalidGrant:
                return RefreshOutcome(FAILED_INVALID_GRANT)
            except RefreshTransientError:
                return RefreshOutcome(FAILED_TRANSIENT)
            expires_at = now_ms + int(tokens["expires_in"]) * 1000
            new_oauth = dict(oauth)
            new_oauth["accessToken"] = tokens["access_token"]
            new_oauth["refreshToken"] = tokens["refresh_token"]
            new_oauth["expiresAt"] = expires_at
            merged = dict(data)
            merged["claudeAiOauth"] = new_oauth
            _atomic_write_creds(creds_path, merged)
            return RefreshOutcome(REFRESHED, expires_at)
    except LockTimeout:
        return RefreshOutcome(FAILED_TRANSIENT)
    except OSError:
        return RefreshOutcome(FAILED_TRANSIENT)
