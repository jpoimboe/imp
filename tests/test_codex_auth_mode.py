import json
from pathlib import Path

from imp.providers.codex_config import detect_auth_mode


def _write(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload))
    return path


def test_auth_mode_field_wins_over_env(tmp_path: Path, monkeypatch) -> None:
    # File says chatgpt, env has OPENAI_API_KEY — file wins (n-3).
    f = _write(tmp_path / "auth.json", {"auth_mode": "chatgpt", "tokens": {}})
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real-key")
    assert detect_auth_mode(canonical_path=f, env={"OPENAI_API_KEY": "sk-real-key"}) == "chatgpt"


def test_auth_mode_apikey_field_wins_over_missing_env(tmp_path: Path) -> None:
    # File says apikey even though env has no key — file wins (n-3).
    f = _write(tmp_path / "auth.json",
               {"auth_mode": "apikey", "OPENAI_API_KEY": "sk-from-file"})
    assert detect_auth_mode(canonical_path=f, env={}) == "apikey"


def test_no_auth_mode_field_falls_back_to_key_presence(tmp_path: Path) -> None:
    # No auth_mode field: corroborate via OPENAI_API_KEY env presence.
    f = _write(tmp_path / "auth.json", {"tokens": {"refresh_token": "rt"}})
    assert detect_auth_mode(canonical_path=f, env={}) == "chatgpt"
    assert detect_auth_mode(canonical_path=f, env={"OPENAI_API_KEY": "sk-"}) == "apikey"


def test_no_auth_file_and_no_env_returns_none(tmp_path: Path) -> None:
    # No usable auth — caller fails fast with an `auth` ProviderError before
    # building SDK options (run() flow step 3).
    assert detect_auth_mode(canonical_path=tmp_path / "missing", env={}) is None


def test_no_auth_file_but_env_key_returns_apikey(tmp_path: Path) -> None:
    assert detect_auth_mode(
        canonical_path=tmp_path / "missing",
        env={"OPENAI_API_KEY": "sk-only-env"},
    ) == "apikey"
