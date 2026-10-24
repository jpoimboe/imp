# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""The shared provider-credentials copy every active sandbox reads: a projection of the
canonical Claude/Codex credentials holding access tokens only, never a refresh token."""
from __future__ import annotations

import contextlib
import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import filelock

from imp.engine.env import ensure_provider_auth_dir
from imp.providers import claude_token_refresh, codex_token_refresh
from imp.providers.codex_config import detect_auth_mode, redacted_auth_json

_STAGE_LOCK_TIMEOUT = 30.0   # the refresh modules' _LOCK_TIMEOUT
_UNSET_SLOW_TIMEOUT_S = 300   # run-verify.sh's fallback for an unset test_cmd_slow_timeout
_WINDOW_SLACK_S = 300
_PROVIDERS = (("claude", claude_token_refresh), ("codex", codex_token_refresh))
# Both refresh modules use these outcome strings.
_REFRESH_FAILURES = {
    claude_token_refresh.FAILED_INVALID_GRANT: "sign-in expired; log in again",
    claude_token_refresh.FAILED_TRANSIENT: "refresh failed; retrying next step",
}
_STAGE_WARNING = "couldn't update the credentials copy for sandboxed tools; retrying next step"


def freshness_window_ms(slow_timeout_s: int | None) -> int:
    """How long a canonical token must outlive a dispatch: the slow gate plus 5 min."""
    base = _UNSET_SLOW_TIMEOUT_S if slow_timeout_s is None else slow_timeout_s
    return (base + _WINDOW_SLACK_S) * 1000


def refresh_and_stage(
    *, now_ms: int, window_ms: int, env: Mapping[str, str], target: Path,
) -> list[str]:
    """Force-refresh each canonical token expiring within `window_ms`, then reconcile the
    staged copy. Returns one warning per failure; only provisioning `target` raises."""
    ensure_provider_auth_dir(target)
    warnings: list[str] = []
    for name, module in _PROVIDERS:
        expires_at = module.canonical_expires_at(env=env)
        if expires_at is None or expires_at - now_ms > window_ms:
            continue
        outcome = module.refresh_canonical_token(
            now_ms, env=env, force=True, spawn_expires_at=expires_at).outcome
        if (reason := _REFRESH_FAILURES.get(outcome)) is not None:
            warnings.append(f"couldn't refresh {name} credentials for sandboxed tools: {reason}")
    try:
        stage_provider_auth(target, env=env)
    except (OSError, RuntimeError):
        warnings.append(_STAGE_WARNING)
    return warnings


def stage_provider_auth(target: Path, *, env: Mapping[str, str]) -> None:
    """Make `target` (already provisioned) equal the projection of the canonical
    credentials. Canonical is read under the lock, so a reconcile never stages a copy
    older than a peer's. Raises OSError (filelock.Timeout included) or RuntimeError (no
    resolvable home); each staged file stays whole."""
    with _stage_lock(target):
        # Every writer holds the lock, so a leftover temp file is a crash orphan.
        for provider, _ in _PROVIDERS:
            for orphan in (target / provider).glob("*.tmp-*"):
                orphan.unlink(missing_ok=True)
        for rel, content in _projection(env).items():
            path = target / rel
            if content is None:
                path.unlink(missing_ok=True)
            elif _read_optional(path) != content:
                _atomic_write(path, content)


def _stage_lock(target: Path) -> filelock.BaseFileLock:
    # A sibling outside the read-only bind: flock needs no write access, so a lock a
    # sandbox could open would let it stall every dispatch.
    return filelock.FileLock(
        str(target.with_name(target.name + ".lock")), timeout=_STAGE_LOCK_TIMEOUT)


def _projection(env: Mapping[str, str]) -> dict[str, bytes | None]:
    """Staged path (relative to the target) → bytes; None means it must be absent."""
    config_override = env.get("CLAUDE_CONFIG_DIR")
    claude_json = (Path(config_override) if config_override else Path.home()) / ".claude.json"
    return {
        "claude/.credentials.json": _claude_credentials(
            claude_token_refresh.canonical_dir(env) / ".credentials.json", env),
        "claude/.claude.json": _read_optional(claude_json),
        "codex/auth.json": _codex_auth(
            codex_token_refresh.canonical_dir(env) / "auth.json", env),
    }


def _claude_credentials(path: Path, env: Mapping[str, str]) -> bytes | None:
    raw = _read_optional(path)
    if not (env.get("CLAUDE_CONFIG_DIR") or env.get("ANTHROPIC_API_KEY")
            or env.get("CLAUDE_CODE_OAUTH_TOKEN")):
        # The SDK's own source rule: macOS keeps the default login in the Keychain.
        from claude_agent_sdk._internal.session_resume import _read_keychain_credentials
        keychain = _read_keychain_credentials()
        if keychain is not None:
            raw = keychain.encode("utf-8")
    creds = _parse_object(raw)
    oauth = creds.get("claudeAiOauth") if creds is not None else None
    if not isinstance(oauth, dict):
        return None
    return _dump({"claudeAiOauth": {k: v for k, v in oauth.items() if k != "refreshToken"}})


def _codex_auth(path: Path, env: Mapping[str, str]) -> bytes | None:
    auth = _parse_object(_read_optional(path))
    if (auth is None or not isinstance(auth.get("tokens"), dict)
            or detect_auth_mode(canonical_path=path, env=env) != "chatgpt"):
        return None
    return redacted_auth_json(auth)


def _parse_object(raw: bytes | None) -> dict[str, Any] | None:
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _read_optional(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _dump(obj: dict[str, Any]) -> bytes:
    return json.dumps(obj, indent=2, sort_keys=True).encode("utf-8")


def _atomic_write(path: Path, content: bytes) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.tmp-")  # 0o600
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(content)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
