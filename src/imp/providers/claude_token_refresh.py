"""Best-effort refresh of the canonical Claude OAuth access token.

Keeps <canonical>/.credentials.json fresh so imp-spawned (redacted) claude
subprocesses always copy a live access token. Never raises: every failure is
reported as a RefreshOutcome. No token material is ever returned or logged.
"""
from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REFRESHED = "refreshed"
SKIPPED_FRESH = "skipped_fresh"
NOT_APPLICABLE = "not_applicable"
FAILED_TRANSIENT = "failed_transient"
FAILED_INVALID_GRANT = "failed_invalid_grant"

_BUFFER_MS = 5 * 60 * 1000


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


def refresh_canonical_token(
    now_ms: int, *, env: Mapping[str, str] | None = None,
) -> RefreshOutcome:
    env = os.environ if env is None else env
    if env.get("ANTHROPIC_API_KEY") or env.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return RefreshOutcome(NOT_APPLICABLE)
    creds_path = _canonical_dir(env) / ".credentials.json"
    oauth = _oauth_of(_load_credentials(creds_path))
    if oauth is None or _refresh_token_of(oauth) is None:
        return RefreshOutcome(NOT_APPLICABLE)
    if not _needs_refresh(oauth, now_ms):
        return RefreshOutcome(SKIPPED_FRESH)
    raise NotImplementedError("refresh path implemented in Task 2")
