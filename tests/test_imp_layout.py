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
    cfg = yaml.safe_load((tmp_path / "imp" / "config.yaml").read_text())
    # New shape: providers is a list, tiers is the tier table.
    assert cfg["providers"] == ["anthropic"]
    assert set(cfg["tiers"]) == {"low", "medium", "high"}
    assert "roles" not in cfg
    # provider_envs uses the default ANTHROPIC_API_KEY (env key omitted from
    # file when blank input was given → uses default).
    if "provider_envs" in cfg:
        assert cfg["provider_envs"]["anthropic"]["env"] == "ANTHROPIC_API_KEY"
