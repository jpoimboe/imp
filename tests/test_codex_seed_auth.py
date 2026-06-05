import json
from pathlib import Path

from imp.providers.codex_config import seed_auth


def test_seed_chatgpt_copies_in_canonical(tmp_path: Path) -> None:
    canonical = tmp_path / "canonical-auth.json"
    canonical.write_text(json.dumps({
        "auth_mode": "chatgpt",
        "tokens": {"refresh_token": "rt-1", "access_token": "at-1",
                   "last_refresh": "2026-06-01T00:00:00Z"},
    }))
    home = tmp_path / "codex-home"
    home.mkdir(mode=0o700)
    out = seed_auth(mode="chatgpt", canonical_path=canonical, codex_home=home, env={})
    assert out == home / "auth.json"
    body = json.loads(out.read_text())
    assert body["tokens"]["refresh_token"] == "rt-1"
    assert (out.stat().st_mode & 0o777) == 0o600


def test_seed_apikey_writes_fresh_auth(tmp_path: Path) -> None:
    home = tmp_path / "codex-home"
    home.mkdir(mode=0o700)
    out = seed_auth(
        mode="apikey", canonical_path=tmp_path / "missing",
        codex_home=home, env={"OPENAI_API_KEY": "sk-real-key"},
    )
    body = json.loads(out.read_text())
    assert body["auth_mode"] == "apikey"
    assert body["OPENAI_API_KEY"] == "sk-real-key"
    assert (out.stat().st_mode & 0o777) == 0o600
