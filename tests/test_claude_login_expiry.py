"""The ~30-day /login window: reading it, and the three-day warning it drives."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from imp.providers import claude_token_refresh as ctr

NOW = 1_000_000_000_000
DAY = 24 * 60 * 60 * 1000
RENEW = "run `claude` then `/login` to renew"


@pytest.mark.parametrize(("remaining_ms", "expected"), [
    (10 * DAY, None),
    (4 * DAY, None),
    (3 * DAY + 1, None),                                            # just outside the window
    (3 * DAY, f"Claude login expires in 3 days — {RENEW}"),         # exactly on it
    (2 * DAY + DAY // 2, f"Claude login expires in 2 days — {RENEW}"),   # floors, not rounds
    (2 * DAY, f"Claude login expires in 2 days — {RENEW}"),
    (DAY, f"Claude login expires in 1 day — {RENEW}"),              # singular noun at 24h
    (DAY - 1, f"Claude login expires in under a day — {RENEW}"),
    (DAY // 2, f"Claude login expires in under a day — {RENEW}"),
    (1, f"Claude login expires in under a day — {RENEW}"),
    (0, f"Claude login has expired — {RENEW}"),
    (-DAY, f"Claude login has expired — {RENEW}"),
])
def test_login_expiry_warning_boundaries(
        remaining_ms: int, expected: str | None) -> None:
    assert ctr.login_expiry_warning(NOW, NOW + remaining_ms) == expected


def test_no_known_expiry_is_silent() -> None:
    assert ctr.login_expiry_warning(NOW, None) is None


def _write_login(base: Path, value: Any, *, key: str = "refreshTokenExpiresAt",
                 oauth_key: str = "claudeAiOauth") -> None:
    oauth: dict[str, Any] = {"accessToken": "old-access", "refreshToken": "old-refresh"}
    if value is not None:
        oauth[key] = value
    (base / ".credentials.json").write_text(
        json.dumps({oauth_key: oauth}), encoding="utf-8")


def _env(base: Path, **extra: str) -> dict[str, str]:
    env = {"CLAUDE_CONFIG_DIR": str(base)}
    env.update(extra)
    return env


def test_reads_the_refresh_window_timestamp(tmp_path: Path) -> None:
    _write_login(tmp_path, NOW + 5 * DAY)
    assert ctr.login_expires_at(env=_env(tmp_path)) == NOW + 5 * DAY


def test_float_timestamp_is_coerced_to_int(tmp_path: Path) -> None:
    _write_login(tmp_path, float(NOW + 5 * DAY))
    result = ctr.login_expires_at(env=_env(tmp_path))
    assert result == NOW + 5 * DAY
    assert isinstance(result, int)


def test_expiry_read_needs_no_token_material(tmp_path: Path) -> None:
    """Only the timestamp is read — a file carrying no tokens still answers."""
    (tmp_path / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"refreshTokenExpiresAt": NOW + 5 * DAY}}),
        encoding="utf-8")
    assert ctr.login_expires_at(env=_env(tmp_path)) == NOW + 5 * DAY


def test_missing_file_is_none(tmp_path: Path) -> None:
    assert ctr.login_expires_at(env=_env(tmp_path)) is None


def test_unreadable_credentials_path_is_none(tmp_path: Path) -> None:
    """A directory in the file's place is a root-proof stand-in for an OSError read."""
    (tmp_path / ".credentials.json").mkdir()
    assert ctr.login_expires_at(env=_env(tmp_path)) is None


def test_torn_file_is_none(tmp_path: Path) -> None:
    (tmp_path / ".credentials.json").write_text("{ not json", encoding="utf-8")
    assert ctr.login_expires_at(env=_env(tmp_path)) is None


def test_missing_oauth_block_is_none(tmp_path: Path) -> None:
    _write_login(tmp_path, NOW + 5 * DAY, oauth_key="somethingElse")
    assert ctr.login_expires_at(env=_env(tmp_path)) is None


def test_missing_timestamp_is_none(tmp_path: Path) -> None:
    _write_login(tmp_path, None)
    assert ctr.login_expires_at(env=_env(tmp_path)) is None


@pytest.mark.parametrize(
    "value",
    ["1700000000000", True, None, [1], {"a": 1}, float("nan"), float("inf"), float("-inf")],
    ids=["str", "bool", "null", "list", "dict", "nan", "inf", "-inf"],
)
def test_non_numeric_timestamp_is_none(tmp_path: Path, value: Any) -> None:
    (tmp_path / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"refreshTokenExpiresAt": value}}),
        encoding="utf-8")
    assert ctr.login_expires_at(env=_env(tmp_path)) is None


@pytest.mark.parametrize("var", ["ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"])
def test_env_credential_suppresses_the_read(tmp_path: Path, var: str) -> None:
    _write_login(tmp_path, NOW + 5 * DAY)
    assert ctr.login_expires_at(env=_env(tmp_path, **{var: "x"})) is None
