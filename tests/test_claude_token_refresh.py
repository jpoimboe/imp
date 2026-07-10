from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

import filelock
import httpx
import pytest

from imp.providers import claude_token_refresh as ctr

NOW = 1_000_000_000_000


def _write_creds(
    base: Path,
    *,
    expires_at: int | None,
    refresh_token: str = "old-refresh",
    access_token: str = "old-access",
) -> Path:
    oauth: dict[str, Any] = {
        "accessToken": access_token,
        "refreshToken": refresh_token,
        "scopes": ["user:inference", "user:profile"],
        "subscriptionType": "max",
    }
    if expires_at is not None:
        oauth["expiresAt"] = expires_at
    path = base / ".credentials.json"
    path.write_text(json.dumps({"claudeAiOauth": oauth}), encoding="utf-8")
    return path


def _env(base: Path, **extra: str) -> dict[str, str]:
    env = {"CLAUDE_CONFIG_DIR": str(base)}
    env.update(extra)
    return env


def test_fresh_token_skips(tmp_path: Path) -> None:
    path = _write_creds(tmp_path, expires_at=NOW + 10 * 60 * 1000)
    before = path.read_bytes()
    out = ctr.refresh_canonical_token(NOW, env=_env(tmp_path))
    assert out.outcome == ctr.SKIPPED_FRESH
    assert out.expires_at is None
    assert path.read_bytes() == before


def test_env_api_key_is_not_applicable(tmp_path: Path) -> None:
    _write_creds(tmp_path, expires_at=NOW - 1)
    out = ctr.refresh_canonical_token(
        NOW, env=_env(tmp_path, ANTHROPIC_API_KEY="sk-x"))
    assert out.outcome == ctr.NOT_APPLICABLE


def test_env_oauth_token_is_not_applicable(tmp_path: Path) -> None:
    _write_creds(tmp_path, expires_at=NOW - 1)
    out = ctr.refresh_canonical_token(
        NOW, env=_env(tmp_path, CLAUDE_CODE_OAUTH_TOKEN="tok"))
    assert out.outcome == ctr.NOT_APPLICABLE


def test_missing_file_is_not_applicable(tmp_path: Path) -> None:
    out = ctr.refresh_canonical_token(NOW, env=_env(tmp_path))
    assert out.outcome == ctr.NOT_APPLICABLE


def test_torn_file_is_not_applicable(tmp_path: Path) -> None:
    (tmp_path / ".credentials.json").write_text("{ not json", encoding="utf-8")
    out = ctr.refresh_canonical_token(NOW, env=_env(tmp_path))
    assert out.outcome == ctr.NOT_APPLICABLE


def test_no_refresh_token_is_not_applicable(tmp_path: Path) -> None:
    (tmp_path / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "a", "expiresAt": NOW - 1}}),
        encoding="utf-8")
    out = ctr.refresh_canonical_token(NOW, env=_env(tmp_path))
    assert out.outcome == ctr.NOT_APPLICABLE


def test_non_dict_oauth_is_not_applicable(tmp_path: Path) -> None:
    (tmp_path / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": None}), encoding="utf-8")
    out = ctr.refresh_canonical_token(NOW, env=_env(tmp_path))
    assert out.outcome == ctr.NOT_APPLICABLE
