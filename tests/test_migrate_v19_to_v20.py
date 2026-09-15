from __future__ import annotations

import json
from pathlib import Path

from imp.engine import quest as quest_mod
from imp.engine.quest import LAYOUT_VERSION, Quest, _migrate_v19_to_v20
from imp.engine.reincarnation import read_session_owner
from tests.conftest import _seed_control

_TIERS = {"low": {"provider": "claude", "model": "sonnet"},
          "medium": {"provider": "claude", "model": "sonnet"},
          "high": {"provider": "claude", "model": "opus"}}


def _seed_v19(control: Path, cid: str, *, steps: dict | None = None) -> Path:
    """A live home as the engine wrote it before this quest: quest.json with no
    `provider` key, stamped back to v19, and a resolved state map."""
    quest = Quest.create_draft(control, title="", quest_id=cid, base_branch="main")
    state_path = quest.quest_dir / "state" / "state.json"
    raw = json.loads(state_path.read_text())
    raw["resolved_tiers"] = _TIERS
    raw["resolved_steps"] = steps or {}
    state_path.write_text(json.dumps(raw, sort_keys=True))
    meta_path = quest.quest_dir / "quest.json"
    meta = json.loads(meta_path.read_text())
    del meta["provider"]
    meta["layout_version"] = 19
    meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True))
    return quest.quest_dir


def _tx(quest_dir: Path, phase: str, step: str, body: str = '{"m": 1}\n') -> Path:
    d = quest_dir / "steps" / phase / step
    d.mkdir(parents=True, exist_ok=True)
    (d / "transcript.jsonl").write_text(body)
    return d


def _seed_current(quest_dir: Path, transcript_dir: Path) -> None:
    state_path = quest_dir / "state" / "state.json"
    raw = json.loads(state_path.read_text())
    raw["current_step"] = {"transcript_dir": str(transcript_dir)}
    state_path.write_text(json.dumps(raw, sort_keys=True))


def _meta(quest_dir: Path) -> dict:
    return json.loads((quest_dir / "quest.json").read_text())


def test_load_backfills_a_null_pin_and_stamps_the_version(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    qd = _seed_v19(tmp_path, "mv20a")

    quest = Quest.load(tmp_path, "mv20a")       # triggers the migration chain

    assert quest.provider is None
    assert _meta(qd)["provider"] is None
    assert _meta(qd)["layout_version"] == LAYOUT_VERSION


def test_every_live_session_is_labelled_not_just_the_current_step(
        tmp_path: Path) -> None:
    # A `resume:` edge can replay a step that finished earlier, so an unlabelled
    # completed step would be retired where a plain replay was correct.
    _seed_control(tmp_path)
    qd = _seed_v19(tmp_path, "mv20b")
    done = _tx(qd, "p", "a")
    live = _tx(qd, "p", "b")
    _seed_current(qd, live)

    _migrate_v19_to_v20(qd)

    assert read_session_owner(done) == "claude"
    assert read_session_owner(live) == "claude"


def test_a_step_override_labels_that_step_with_its_own_vendor(
        tmp_path: Path) -> None:
    # resolved_steps wins over the tier map, exactly as the dispatcher reads it,
    # so a cross-vendor /model override is labelled with the vendor that ran it.
    _seed_control(tmp_path)
    qd = _seed_v19(tmp_path, "mv20c",
                   steps={"b": {"provider": "openai", "model": "gpt-5"}})
    plain = _tx(qd, "p", "a")
    overridden = _tx(qd, "p", "b")

    _migrate_v19_to_v20(qd)

    assert read_session_owner(plain) == "claude"
    assert read_session_owner(overridden) == "openai"


def test_empty_sessions_and_attempt_stashes_are_left_unlabelled(
        tmp_path: Path) -> None:
    # Nothing replayable and nothing ever read back: labelling either would be
    # noise, and unlabelled is the safe reading anyway.
    _seed_control(tmp_path)
    qd = _seed_v19(tmp_path, "mv20d")
    empty = _tx(qd, "p", "a", body="")
    stash = _tx(qd, "p", "b.attempt-1")

    _migrate_v19_to_v20(qd)

    assert read_session_owner(empty) is None
    assert read_session_owner(stash) is None


def test_a_home_with_no_resolved_map_is_left_unlabelled(tmp_path: Path) -> None:
    # An unresolved home names no vendor to record; unlabelled retires rather
    # than replaying into a vendor that may reject the transcript.
    _seed_control(tmp_path)
    qd = _seed_v19(tmp_path, "mv20e")
    state_path = qd / "state" / "state.json"
    raw = json.loads(state_path.read_text())
    raw["resolved_tiers"] = None
    state_path.write_text(json.dumps(raw, sort_keys=True))
    live = _tx(qd, "p", "a")

    _migrate_v19_to_v20(qd)

    assert read_session_owner(live) is None
    assert _meta(qd)["provider"] is None          # the pin half still lands
    assert _meta(qd)["layout_version"] == 20


def test_the_hop_is_registered_and_the_constant_moved() -> None:
    assert LAYOUT_VERSION == 23
    assert quest_mod._MIGRATIONS[(19, 20)] is _migrate_v19_to_v20


def test_a_second_run_is_a_no_op(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    qd = _seed_v19(tmp_path, "mv20f")
    live = _tx(qd, "p", "a")
    _migrate_v19_to_v20(qd)
    after_first = _meta(qd)

    _migrate_v19_to_v20(qd)

    assert _meta(qd) == after_first
    assert read_session_owner(live) == "claude"
