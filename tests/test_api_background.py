"""imp.api.create_quest(background=True): a detached autonomous run, no attach."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from imp.api import create_quest
from imp.paths import project_state_dir
from tests.conftest import _git, _init_repo, _seed_control, write_agent_md


def _seed_scripted_repo(root: Path) -> None:
    _init_repo(root)
    src = root / "imp" / "workflows" / "imp"
    (src / "agents").mkdir(parents=True)
    write_agent_md(
        src / "agents" / "coder.md",
        frontmatter=(
            "name: coder\ntools: [complete_step]\n"
            "output:\n  status: {type: enum, values: [Success, Error]}"
        ),
    )
    (src / "workflow.yaml").write_text(
        'name: t\ndescription: ""\n'
        "phases:\n  - name: p\n    steps:\n"
        "      - {name: s, file: agents/coder.md}\n"
    )
    (root / "imp" / "config.yaml").write_text(
        "default-workflow: imp\ndefault-base: main\n")
    (project_state_dir(root) / "config.yaml").write_text(
        "providers: [scripted]\n"
        "tiers:\n"
        "  low: {scripted: x}\n"
        "  medium: {scripted: x}\n"
        "  high: {scripted: x}\n"
        "sandbox:\n  enabled: false\n"
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "imp setup")


def _await_archived_status(
    control_root: Path, quest_id: str, want: str, *, backstop: float = 180.0,
) -> str | None:
    """Poll the archive — a completed draft archives, then its live home is reaped."""
    from imp.engine.archive import read as archive_read

    deadline = time.monotonic() + backstop
    last: str | None = None
    while time.monotonic() < deadline:
        last = archive_read.status_of(control_root, quest_id)
        if last == want:
            return last
        time.sleep(0.1)
    return last


def test_background_flag_calls_start_quest_with_the_created_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    started: dict[str, object] = {}
    monkeypatch.setattr(
        "imp.remote.daemon.spawn.start_quest",
        lambda control, quest_id, *, autonomous: started.update(
            control=control, id=quest_id))

    handle = create_quest(tmp_path, "go", background=True)

    assert started == {"control": tmp_path, "id": handle.id}


def test_no_background_flag_spawns_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)

    def _boom(control: Path, quest_id: str, *, autonomous: bool) -> None:
        raise AssertionError("start_quest must not run without background=True")

    monkeypatch.setattr("imp.remote.daemon.spawn.start_quest", _boom)

    create_quest(tmp_path, "go")


@pytest.mark.timeout(300)   # real detached subprocess + git ops under -n auto
def test_background_create_reaches_a_terminal_state_with_no_attach(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The one step completes and nothing else: `_spawn_detached` sends the child's
    # stderr to DEVNULL, so any extra tool call that failed would surface only as
    # this poll expiring with no diagnostic.
    script_path = tmp_path / "script.json"
    script_path.write_text(json.dumps({
        "default": [
            {"text": "done", "tools": [
                {"name": "complete_step",
                 "input": {"outcome": "ok", "status": "Success"}},
            ]},
        ],
    }))
    monkeypatch.setenv("IMP_SCRIPTED_SCRIPT", str(script_path))
    _seed_scripted_repo(tmp_path)

    handle = create_quest(tmp_path, "do the thing", background=True)

    status = _await_archived_status(tmp_path, handle.id, "completed")
    assert status == "completed", (
        f"quest {handle.id} last read {status!r}, never reached 'completed'")
