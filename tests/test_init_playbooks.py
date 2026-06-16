from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from imp.cli.init import run_init


@pytest.fixture(autouse=True)
def _isolate_user_config(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))


@pytest.fixture(autouse=True)
def _stub_theme_detection(monkeypatch):
    monkeypatch.setattr("imp.cli.init._default_detect_theme", lambda: "ansi")


def _run(tmp_path: Path) -> None:
    run_init(
        project_root=tmp_path,
        picker_fn=lambda _available: ["anthropic"],
        input_fn=lambda _prompt="": "",
        output_fn=lambda _msg: None,
    )


def test_init_copies_playbook_library(tmp_path: Path) -> None:
    _run(tmp_path)
    pb_dir = tmp_path / "imp" / "playbooks"
    assert (pb_dir / "ux.md").is_file()
    assert (pb_dir / "interrupt.md").is_file()
    assert (pb_dir / "dead-code.md").is_file()


def test_init_backfills_playbooks_on_partial_tree(tmp_path: Path) -> None:
    _run(tmp_path)
    # Simulate a clone that committed project.yaml but lacks playbooks/.
    shutil.rmtree(tmp_path / "imp" / "playbooks")
    _run(tmp_path)  # idempotent re-run backfills
    assert (tmp_path / "imp" / "playbooks" / "ux.md").is_file()
