"""Part C: best-effort token-expiry diagnostic for claude_code auth errors."""

from __future__ import annotations

import json
from pathlib import Path

from imp.providers import errors as perr
from imp.providers.claude_code import _read_token_expiry, _termination_from_result
from tests.cc_fakes import result

NOW = 1_700_000_000_000  # fixed "now" in epoch ms for deterministic comparisons


def _write_creds(config_dir: Path, expires_at: object) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "x", "expiresAt": expires_at,
                                      "scopes": ["user:inference"]}}),
        encoding="utf-8")


def test_past_expiry_reads_as_expired(tmp_path: Path) -> None:
    _write_creds(tmp_path, NOW - 1000)
    out = _read_token_expiry(tmp_path, NOW)
    assert out == {"token_expires_at": NOW - 1000,
                   "token_expired": True, "checked_at": NOW}


def test_future_expiry_beyond_buffer_reads_as_not_expired(tmp_path: Path) -> None:
    _write_creds(tmp_path, NOW + 3_600_000)  # 1h out, clearly past the buffer
    out = _read_token_expiry(tmp_path, NOW)
    assert out["token_expires_at"] == NOW + 3_600_000
    assert out["token_expired"] is False


def test_expiry_within_five_minute_buffer_reads_as_expired(tmp_path: Path) -> None:
    # 1 minute of life left is inside the CLI's 5-minute refresh window, so the
    # token already counts as expired. Falsifies a bufferless `expires_at <= now`.
    _write_creds(tmp_path, NOW + 60_000)
    out = _read_token_expiry(tmp_path, NOW)
    assert out["token_expired"] is True


def test_missing_file_yields_null_and_does_not_raise(tmp_path: Path) -> None:
    out = _read_token_expiry(tmp_path, NOW)  # tmp_path has no .credentials.json
    assert out == {"token_expires_at": None, "token_expired": None,
                   "checked_at": NOW}


def test_torn_json_yields_null(tmp_path: Path) -> None:
    (tmp_path / ".credentials.json").write_text(
        '{"claudeAiOauth": {"expiresAt": 17', encoding="utf-8")
    out = _read_token_expiry(tmp_path, NOW)
    assert out["token_expires_at"] is None
    assert out["token_expired"] is None


def test_missing_oauth_key_yields_null(tmp_path: Path) -> None:
    (tmp_path / ".credentials.json").write_text("{}", encoding="utf-8")
    out = _read_token_expiry(tmp_path, NOW)
    assert out == {"token_expires_at": None, "token_expired": None,
                   "checked_at": NOW}


def test_bool_expires_at_is_rejected(tmp_path: Path) -> None:
    (tmp_path / ".credentials.json").write_text(
        '{"claudeAiOauth": {"expiresAt": true}}', encoding="utf-8")
    out = _read_token_expiry(tmp_path, NOW)
    assert out["token_expires_at"] is None
    assert out["token_expired"] is None


def test_falls_back_to_home_claude_when_config_dir_none(tmp_path: Path,
                                                        monkeypatch) -> None:
    home = tmp_path / "home"
    _write_creds(home / ".claude", NOW - 5000)
    monkeypatch.setattr(Path, "home", lambda: home)
    out = _read_token_expiry(None, NOW)
    assert out["token_expires_at"] == NOW - 5000
    assert out["token_expired"] is True
