from __future__ import annotations

from pathlib import Path

import pytest

from imp.providers.codex_runner import PreflightError, preflight


def test_no_auth_anywhere_fails_auth(tmp_path: Path) -> None:
    with pytest.raises(PreflightError) as exc:
        preflight(model="gpt-5.5", canonical_auth=tmp_path / "absent", env={})
    assert exc.value.category == "auth"


def test_no_auth_preflight_message_is_unchanged(tmp_path: Path) -> None:
    """Explicitly out of the dead-credential copy rewrite: this message names
    both remedies already and must stay byte-identical."""
    with pytest.raises(PreflightError) as exc:
        preflight(model="gpt-5.5", canonical_auth=tmp_path / "absent", env={})
    assert str(exc.value) == (
        "no usable codex auth (neither ~/.codex/auth.json nor OPENAI_API_KEY); "
        "run `codex login` or export an API key.")


def test_codex_model_under_chatgpt_fails_config(tmp_path: Path) -> None:
    canonical = tmp_path / "auth.json"
    canonical.write_text('{"auth_mode": "chatgpt", "tokens": {"refresh_token": "rt"}}')
    with pytest.raises(PreflightError) as exc:
        preflight(model="gpt-5.5-codex", canonical_auth=canonical, env={})
    assert exc.value.category == "config"
    assert "codex" in str(exc.value).lower()


def test_codex_model_under_apikey_also_fails_config(tmp_path: Path) -> None:
    """M1: `*-codex` is refused under BOTH auth modes."""
    with pytest.raises(PreflightError) as exc:
        preflight(model="gpt-5.5-codex", canonical_auth=tmp_path / "absent",
                  env={"OPENAI_API_KEY": "sk-real"})
    assert exc.value.category == "config"


def test_happy_path_returns_auth_mode(tmp_path: Path) -> None:
    """`tmp_path` is not a git repo — imp's preflight no longer rejects a non-git cwd."""
    canonical = tmp_path / "auth.json"
    canonical.write_text('{"auth_mode": "chatgpt", "tokens": {"refresh_token": "rt"}}')
    mode = preflight(model="gpt-5.5", canonical_auth=canonical, env={})
    assert mode == "chatgpt"
