from __future__ import annotations

import os
from pathlib import Path

import pytest

from imp.providers import codex_token_refresh as ctr
from imp.providers.codex_runner import CodexAgentRunner


def _runner() -> CodexAgentRunner:
    return CodexAgentRunner(
        model_name="gpt-5.5", client_factory=lambda options=None: None)


def test_canonical_auth_path_honors_codex_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    runner = _runner()
    assert runner._canonical_auth_path() == tmp_path / "auth.json"
    # Runner and refresh module resolve the SAME auth.json under a set CODEX_HOME.
    assert (runner._canonical_auth_path()
            == ctr._canonical_dir(os.environ) / "auth.json")


def test_canonical_auth_path_falls_back_to_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    runner = _runner()
    assert runner._canonical_auth_path() == tmp_path / ".codex" / "auth.json"
    assert (runner._canonical_auth_path()
            == ctr._canonical_dir({}) / "auth.json")


def test_canonical_auth_override_still_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "ignored"))
    runner = _runner()
    override = tmp_path / "override" / "auth.json"
    runner._canonical_auth_override = override
    assert runner._canonical_auth_path() == override
