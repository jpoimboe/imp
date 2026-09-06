"""The canonical access token's expiry — the value a rotation race compares."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from imp.providers import claude_token_refresh as ctr

NOW = 1_000_000_000_000
HOUR = 60 * 60 * 1000


def _write_creds(base: Path, oauth: dict[str, Any]) -> None:
    (base / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": oauth}), encoding="utf-8")


def _env(base: Path, **extra: str) -> dict[str, str]:
    env = {"CLAUDE_CONFIG_DIR": str(base)}
    env.update(extra)
    return env


def test_reads_the_access_token_expiry(tmp_path: Path) -> None:
    _write_creds(tmp_path, {"accessToken": "x", "refreshToken": "y",
                            "expiresAt": NOW + HOUR})
    assert ctr.canonical_expires_at(env=_env(tmp_path)) == NOW + HOUR


def test_reads_expires_at_not_the_login_window(tmp_path: Path) -> None:
    """The two readers pick different keys off the same file."""
    _write_creds(tmp_path, {"expiresAt": NOW + HOUR,
                            "refreshTokenExpiresAt": NOW + 30 * 24 * HOUR})
    assert ctr.canonical_expires_at(env=_env(tmp_path)) == NOW + HOUR
    assert ctr.login_expires_at(env=_env(tmp_path)) == NOW + 30 * 24 * HOUR


def test_default_env_reads_the_process_environment(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The provider calls it with no arguments, so os.environ must resolve."""
    _write_creds(tmp_path, {"expiresAt": NOW + HOUR})
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    assert ctr.canonical_expires_at() == NOW + HOUR


def test_expiry_read_needs_no_token_material(tmp_path: Path) -> None:
    _write_creds(tmp_path, {"expiresAt": NOW + HOUR})
    assert ctr.canonical_expires_at(env=_env(tmp_path)) == NOW + HOUR


def test_float_timestamp_is_coerced_to_int(tmp_path: Path) -> None:
    _write_creds(tmp_path, {"expiresAt": float(NOW + HOUR)})
    result = ctr.canonical_expires_at(env=_env(tmp_path))
    assert result == NOW + HOUR
    assert isinstance(result, int)


def test_missing_file_is_none(tmp_path: Path) -> None:
    assert ctr.canonical_expires_at(env=_env(tmp_path)) is None


def test_unreadable_credentials_path_is_none(tmp_path: Path) -> None:
    """A directory in the file's place is a root-proof stand-in for an OSError read."""
    (tmp_path / ".credentials.json").mkdir()
    assert ctr.canonical_expires_at(env=_env(tmp_path)) is None


def test_torn_file_is_none(tmp_path: Path) -> None:
    (tmp_path / ".credentials.json").write_text(
        '{"claudeAiOauth": {"expiresAt": 17', encoding="utf-8")
    assert ctr.canonical_expires_at(env=_env(tmp_path)) is None


def test_missing_oauth_block_is_none(tmp_path: Path) -> None:
    (tmp_path / ".credentials.json").write_text(
        json.dumps({"somethingElse": {"expiresAt": NOW + HOUR}}), encoding="utf-8")
    assert ctr.canonical_expires_at(env=_env(tmp_path)) is None


def test_missing_timestamp_is_none(tmp_path: Path) -> None:
    _write_creds(tmp_path, {"accessToken": "x"})
    assert ctr.canonical_expires_at(env=_env(tmp_path)) is None


@pytest.mark.parametrize(
    "value",
    ["1700000000000", True, None, [1], {"a": 1},
     float("nan"), float("inf"), float("-inf")],
    ids=["str", "bool", "null", "list", "dict", "nan", "inf", "-inf"],
)
def test_non_finite_or_non_numeric_timestamp_is_none(
        tmp_path: Path, value: Any) -> None:
    _write_creds(tmp_path, {"expiresAt": value})
    assert ctr.canonical_expires_at(env=_env(tmp_path)) is None


@pytest.mark.parametrize("var", ["ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"])
def test_env_credential_suppresses_the_read(tmp_path: Path, var: str) -> None:
    _write_creds(tmp_path, {"expiresAt": NOW + HOUR})
    assert ctr.canonical_expires_at(env=_env(tmp_path, **{var: "x"})) is None
