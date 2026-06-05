from __future__ import annotations

from pathlib import Path

import pytest

from imp.providers.codex_runner import PreflightError, preflight


def _git_init(p: Path) -> None:
    import subprocess
    subprocess.run(["git", "init", "-q", str(p)], check=True)


def test_no_auth_anywhere_fails_auth(tmp_path: Path) -> None:
    _git_init(tmp_path)
    with pytest.raises(PreflightError) as exc:
        preflight(model="gpt-5.5", cwd=tmp_path,
                  canonical_auth=tmp_path / "absent", env={})
    assert exc.value.category == "auth"


def test_codex_model_under_chatgpt_fails_config(tmp_path: Path) -> None:
    _git_init(tmp_path)
    canonical = tmp_path / "auth.json"
    canonical.write_text('{"auth_mode": "chatgpt", "tokens": {"refresh_token": "rt"}}')
    with pytest.raises(PreflightError) as exc:
        preflight(model="gpt-5.5-codex", cwd=tmp_path,
                  canonical_auth=canonical, env={})
    assert exc.value.category == "config"
    assert "codex" in str(exc.value).lower()


def test_codex_model_under_apikey_also_fails_config(tmp_path: Path) -> None:
    """M1: `*-codex` is refused under BOTH auth modes."""
    _git_init(tmp_path)
    with pytest.raises(PreflightError) as exc:
        preflight(model="gpt-5.5-codex", cwd=tmp_path,
                  canonical_auth=tmp_path / "absent",
                  env={"OPENAI_API_KEY": "sk-real"})
    assert exc.value.category == "config"


def test_non_git_cwd_fails_config(tmp_path: Path) -> None:
    """M2: fail-closed revert has no substrate without git."""
    with pytest.raises(PreflightError) as exc:
        preflight(model="gpt-5.5", cwd=tmp_path,
                  canonical_auth=tmp_path / "absent",
                  env={"OPENAI_API_KEY": "sk-real"})
    assert exc.value.category == "config"
    assert "git" in str(exc.value).lower()


def test_happy_path_returns_auth_mode(tmp_path: Path) -> None:
    _git_init(tmp_path)
    canonical = tmp_path / "auth.json"
    canonical.write_text('{"auth_mode": "chatgpt", "tokens": {"refresh_token": "rt"}}')
    mode = preflight(model="gpt-5.5", cwd=tmp_path,
                     canonical_auth=canonical, env={})
    assert mode == "chatgpt"
