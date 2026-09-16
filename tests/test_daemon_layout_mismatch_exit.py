from __future__ import annotations

import json
from pathlib import Path

import pytest

from imp.engine.quest import LAYOUT_VERSION
from imp.paths import project_state_dir
from imp.remote.session import daemon
from imp.remote.session.attach import LAYOUT_MISMATCH_EXIT
from tests.conftest import _seed_control


def _seed_downgraded_home(control: Path, cid: str) -> None:
    """A home stamped one version FORWARD of this engine (stored > engine): a
    genuine downgrade, which Quest.load's stored-version guard rejects with
    LayoutMismatch."""
    quest_dir = project_state_dir(control) / "quests" / cid
    quest_dir.mkdir(parents=True, exist_ok=True)
    (quest_dir / "quest.json").write_text(json.dumps({
        "id": cid,
        "title": "T",
        "base_branch": "main",
        "created_at": "2026-01-01T00:00:00+00:00",
        "create_mode": "new",
        "depends_on": [],
        "project_root": str(control),
        "materialized": True,
        "layout_version": LAYOUT_VERSION + 1,
        "imp_commit": "",
        "workflow": None,
        "origin": "draft",
        "seed_opening": None,
        "parent": None,
    }, indent=2, sort_keys=True))


def test_daemon_main_exits_layout_mismatch_code_and_prints_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_control(tmp_path)
    _seed_downgraded_home(tmp_path, "feat")
    rc = daemon.main(["--control-root", str(tmp_path), "--quest-id", "feat"])
    assert rc == LAYOUT_MISMATCH_EXIT
    err = capsys.readouterr().err
    assert f"v{LAYOUT_VERSION + 1}" in err and f"v{LAYOUT_VERSION}" in err
    assert "Update imp" in err
    assert "feat" in err                       # quest id names the culprit
