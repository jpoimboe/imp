"""Tests for imp/ committed tree layout written by run_init."""

from __future__ import annotations

from pathlib import Path

import yaml

from imp.cli.init import run_init


def _init(tmp_path: Path) -> None:
    run_init(project_root=tmp_path, input_fn=lambda _p: "",
             output_fn=lambda _l: None, picker_fn=lambda _a: ["anthropic"])


def test_init_writes_committed_imp_tree(tmp_path: Path) -> None:
    _init(tmp_path)
    assert (tmp_path / "imp" / "config.yaml").exists()
    assert (tmp_path / "imp" / "workflows" / "default" / "workflow.yaml").exists()
    # config has only env-var NAMES, never key values (spec §8.1).
    cfg = yaml.safe_load((tmp_path / "imp" / "config.yaml").read_text())
    assert cfg["providers"]["anthropic"]["env"] == "ANTHROPIC_API_KEY"
    assert "roles" in cfg
