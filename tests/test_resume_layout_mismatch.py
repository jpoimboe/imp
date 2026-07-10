from __future__ import annotations

import argparse
import json
from pathlib import Path

import anyio

import imp.cli.main as m
from imp.engine.quest import LAYOUT_VERSION, Quest
from tests.conftest import _seed_control


def test_resume_layout_mismatch_prints_actionable_not_resume_gone(
    tmp_path: Path, monkeypatch, capsys,
) -> None:
    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")
    meta_path = ch.quest_dir / "quest.json"
    meta = json.loads(meta_path.read_text())
    meta["layout_version"] = 999                 # contradictory stored version
    meta["imp_commit"] = "cafef00d"
    meta_path.write_text(json.dumps(meta))
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)

    async def _fail_client(*_a, **_k) -> int:
        raise AssertionError("LayoutMismatch must short-circuit before the session client")

    monkeypatch.setattr(m, "run_session_client", _fail_client)

    rc = anyio.run(m._async_cmd_resume,
                   argparse.Namespace(token="feat"))

    err = capsys.readouterr().err
    assert rc == 2
    assert "999" in err and str(LAYOUT_VERSION) in err     # both versions surfaced
    assert "cafef00d" in err                                # imp_commit breadcrumb
    assert "imp log" in err                                  # recovery recipe
    assert "no Quest matches" not in err                   # did NOT fall to _resume_gone
