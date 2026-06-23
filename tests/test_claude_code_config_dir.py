"""materialize_fresh_config_dir seeds an isolated, auth-bearing temp config dir."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from imp.providers.claude_code_config_dir import materialize_fresh_config_dir


def test_creates_isolated_projects_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # No credentials at the source → nothing to seed, but the layout is created.
    src = tmp_path / "empty_home"
    src.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(src))
    cfg = materialize_fresh_config_dir()
    try:
        assert cfg.is_dir()
        assert (cfg / "projects").is_dir()
        assert not (cfg / ".credentials.json").exists()
    finally:
        shutil.rmtree(cfg, ignore_errors=True)


def test_seeds_redacted_credentials_from_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The fresh subprocess runs under a redirected home; the single-use
    # refreshToken must be stripped so it can't be consumed and revoke the
    # parent's stored creds (SDK resume semantics).
    src = tmp_path / "src_home"
    src.mkdir()
    (src / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "keep-me", "refreshToken": "secret"}})
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(src))
    cfg = materialize_fresh_config_dir()
    try:
        seeded = json.loads((cfg / ".credentials.json").read_text())
        assert seeded["claudeAiOauth"]["accessToken"] == "keep-me"
        assert "refreshToken" not in seeded["claudeAiOauth"]
    finally:
        shutil.rmtree(cfg, ignore_errors=True)
