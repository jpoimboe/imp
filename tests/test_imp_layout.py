"""Tests for imp/ committed tree layout written by run_init."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from imp.cli.init import run_init


@pytest.fixture(autouse=True)
def _isolate_user_config(monkeypatch, tmp_path):
    # init bootstraps the user config — pin XDG into the tmp tree.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr("imp.cli.init._default_detect_theme", lambda: "ansi")


def _init(tmp_path: Path) -> None:
    run_init(project_root=tmp_path, input_fn=lambda _p: "",
             output_fn=lambda _l: None, picker_fn=lambda _a: ["anthropic"])


def test_init_writes_committed_imp_tree(tmp_path: Path) -> None:
    _init(tmp_path)
    assert (tmp_path / "imp" / "project.yaml").exists()
    assert (tmp_path / "imp" / "workflows" / "imp" / "workflow.yaml").exists()
    proj = yaml.safe_load((tmp_path / "imp" / "project.yaml").read_text())
    # New shape: a workflow ref + empty vars. Bindings/providers are NOT here
    # (the floor owns tiers; providers are user-homed).
    assert proj["default-workflow"] == "imp"
    assert "tiers" not in proj
    assert "providers" not in proj
    assert "roles" not in proj
