"""imp status prints the one-line update notice when the managed workflow lags."""
from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from imp.assets import bundled_workflow_dir
from imp.cli.main import _cmd_status
from imp.engine.workflow_lifecycle import write_pristine
from tests.conftest import _git, _init_repo


def test_status_prints_update_notice(tmp_path, monkeypatch, capsys) -> None:
    _init_repo(tmp_path)
    (tmp_path / "imp").mkdir()
    (tmp_path / "imp" / "project.yaml").write_text("default-workflow: imp\n")
    mirror = tmp_path / "imp" / "workflows" / "imp"
    shutil.copytree(bundled_workflow_dir("imp"), mirror)
    (mirror / "workflow.yaml").write_text("name: stale-mirror\n")
    write_pristine(mirror)
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "stale mirror")
    monkeypatch.chdir(tmp_path)

    rc = _cmd_status(argparse.Namespace(all=False))
    assert rc == 0
    assert "update-workflow" in capsys.readouterr().out


def test_new_cmd_prints_update_notice(tmp_path: Path, monkeypatch, capsys) -> None:
    """_async_cmd_new emits the update notice before Change.create is called."""
    import asyncio

    from imp.engine.change import Change
    from imp.engine.errors import LoadError
    from tests.conftest import _seed_control

    _seed_control(tmp_path)
    # Overwrite the workflow mirror with a stale copy that has a .pristine stamp.
    mirror = tmp_path / "imp" / "workflows" / "imp"
    (mirror / "workflow.yaml").write_text("name: stale-mirror\n")
    write_pristine(mirror)
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "stale mirror")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.chdir(tmp_path)

    # Stub Change.create so we don't need a full worktree but do get the notice.
    monkeypatch.setattr(
        Change, "create",
        staticmethod(lambda *a, **kw: (_ for _ in ()).throw(LoadError("stub"))),
    )

    from imp.cli.main import _async_cmd_new

    rc = asyncio.run(_async_cmd_new(argparse.Namespace(
        title=None, seed=None, message=None, tui=False, non_interactive=True,
    )))
    # rc is 2 because Change.create raises LoadError, but the notice was already printed.
    assert rc == 2
    out = capsys.readouterr().out
    assert "update-workflow" in out
