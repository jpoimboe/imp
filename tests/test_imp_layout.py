"""Tests for imp/ committed tree layout written by run_init."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from imp.cli.init import run_init


@pytest.fixture(autouse=True)
def _isolate_global_config(monkeypatch, tmp_path):
    # init bootstraps the global config — pin IMP_HOME into the tmp tree.
    monkeypatch.setenv("IMP_HOME", str(tmp_path / "imphome"))
    # This override shadows conftest's autouse fixture for the module, so it must
    # neuter the migration's legacy source too.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".xdg-isolated"))
    monkeypatch.setattr("imp.cli.init._default_detect_theme", lambda: "ansi")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@t")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@t")
    import imp.cli.init as initmod
    from imp.agents.interactive import InteractiveSuccess
    monkeypatch.setattr(initmod, "sandbox_available", lambda: True, raising=False)
    monkeypatch.setattr(initmod, "resolve_detection_provider",
                        lambda _p, **_kw: ("anthropic", "m"), raising=False)
    monkeypatch.setattr(initmod, "_run_init_conversation", lambda **kw: InteractiveSuccess({
        "status": "Success", "git_init": True, "default_workflow": "default",
        "default_base": "main", "test_cmd_fast": "", "pythonpath": {"roots": []}}),
        raising=False)


def _init(tmp_path: Path) -> None:
    run_init(project_root=tmp_path, input_fn=lambda _p: "",
             output_fn=lambda _l: None, picker_fn=lambda _a: ["anthropic"])


def test_init_writes_committed_imp_tree(tmp_path: Path) -> None:
    _init(tmp_path)
    assert (tmp_path / "imp" / "project.yaml").exists()
    assert not (tmp_path / "imp" / "workflows").exists()
    proj = yaml.safe_load((tmp_path / "imp" / "project.yaml").read_text())
    # New shape: a workflow ref + empty vars. Bindings/providers are NOT here
    # (the floor owns tiers; providers are global-homed).
    assert proj["default-workflow"] == "default"
    assert "tiers" not in proj
    assert "providers" not in proj
    assert "roles" not in proj
