from __future__ import annotations

import json
from pathlib import Path

from imp.engine.quest import LAYOUT_VERSION, Quest, _migrate_v10_to_v11
from tests.conftest import _seed_control


def _seed_v10(control: Path, cid: str, *, resume_target: str | None) -> Path:
    """A live home stamped back to v10 with a hand-written state.json carrying the
    pre-split resume_target vocabulary."""
    ch = Quest.create(control, title="", quest_id=cid, base_branch="main")
    (ch.state_dir / "state.json").write_text(json.dumps({
        "id": cid, "step": "code", "step_attempt": 0, "status": "paused",
        "resume_target": resume_target, "vars": {}, "loop_counts": {},
        "current_step": None, "retry_feedback": None,
    }, indent=2, sort_keys=True))
    meta_path = ch.quest_dir / "quest.json"
    meta = json.loads(meta_path.read_text())
    meta["layout_version"] = 10
    meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True))
    return ch.quest_dir


def _state(quest_dir: Path) -> dict:
    return json.loads((quest_dir / "state" / "state.json").read_text())


def _meta(quest_dir: Path) -> dict:
    return json.loads((quest_dir / "quest.json").read_text())


def test_prompt_becomes_prompt_paused(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    qd = _seed_v10(tmp_path, "mv1", resume_target="prompt")
    _migrate_v10_to_v11(qd)
    assert _state(qd)["resume_target"] == "prompt-paused"
    assert _meta(qd)["layout_version"] == 11


def test_agent_target_is_unchanged(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    qd = _seed_v10(tmp_path, "mv2", resume_target="agent")
    _migrate_v10_to_v11(qd)
    assert _state(qd)["resume_target"] == "agent"


def test_absent_target_is_unchanged(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    qd = _seed_v10(tmp_path, "mv3", resume_target=None)
    _migrate_v10_to_v11(qd)
    assert _state(qd)["resume_target"] is None
    assert _meta(qd)["layout_version"] == 11


def test_unmaterialized_seed_advances_version_only(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    qd = _seed_v10(tmp_path, "mv4", resume_target="prompt")
    (qd / "state" / "state.json").unlink()   # un-materialized: no state.json
    _migrate_v10_to_v11(qd)
    assert _meta(qd)["layout_version"] == 11
    assert not (qd / "state" / "state.json").exists()


def test_load_migrates_v10_home_end_to_end(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    qd = _seed_v10(tmp_path, "mv5", resume_target="prompt")
    Quest.load(tmp_path, "mv5")               # triggers the migration chain on load
    assert _state(qd)["resume_target"] == "prompt-paused"
    assert _meta(qd)["layout_version"] == 12


def test_hop_is_idempotent_and_pins_layout_version(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    qd = _seed_v10(tmp_path, "mv6", resume_target="prompt")
    _migrate_v10_to_v11(qd)
    _migrate_v10_to_v11(qd)                   # second run must not corrupt or re-change
    assert _state(qd)["resume_target"] == "prompt-paused"
    assert not list((qd / "state").glob(".state-*")) and not list(qd.glob(".quest-*"))
    assert LAYOUT_VERSION == 12
