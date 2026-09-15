from __future__ import annotations

import json
from pathlib import Path

import pytest

from imp.engine import quest as quest_mod
from imp.engine.executor import _retire_foreign_session
from imp.engine.quest import LAYOUT_VERSION, Quest, _migrate_v22_to_v23
from imp.engine.reincarnation import read_session_owner
from imp.engine.resume_state import (
    RESUME_STATE_NAME,
    InvalidResumeState,
    NativeResumeState,
    encode_native_resume_state,
    read_native_resume_state,
)
from imp.engine.state import State
from imp.engine.types import Pause, SourceLoc, Step
from tests.conftest import _seed_control

_EVENTS = [
    {"type": "step_context", "agent_id": "step:p/s",
     "payload": {"provider": "claude_code", "model": "opus"}},
    {"type": "agent_message", "agent_id": "step:p/s",
     "payload": {"provider": "claude_code", "model": "opus",
                 "raw": {"provider": "claude_code", "text": "hi"}}},
    {"type": "quota_reading", "agent_id": None,
     "payload": {"provider": "claude_code", "session_pct": 62}},
    {"type": "step_started", "agent_id": None,
     "payload": {"step": "s", "phase": "p", "provider": "claude_code", "round": 1}},
    {"type": "tiers_resolved", "agent_id": None,
     "payload": {"tiers": {"medium": {"provider": "claude_code", "model": "sonnet"}},
                 "steps": {"s": {"provider": "claude_code", "model": "opus"}}}},
]


def _seed_v22(control: Path, cid: str, *, provider: str = "claude_code") -> Path:
    """A live home as the engine wrote it before this quest: the old id in all five
    locations of the §9b table, stamped back to v22."""
    quest = Quest.create_draft(control, title="", quest_id=cid, base_branch="main")
    qd = quest.quest_dir
    state_path = qd / "state" / "state.json"
    raw = json.loads(state_path.read_text())
    raw["resolved_tiers"] = {"medium": {"provider": provider, "model": "sonnet"}}
    raw["resolved_steps"] = {"s": {"provider": provider, "model": "opus"}}
    # Compact, unlike _rewrite_state_json's indent=2: an unconditional rewrite
    # would change these bytes, which the no-old-id case asserts it does not.
    state_path.write_text(json.dumps(raw, sort_keys=True))
    events = [json.loads(json.dumps(e).replace("claude_code", provider))
              for e in _EVENTS]
    (qd / "events.jsonl").write_text(
        "".join(json.dumps(e) + "\n" for e in events))
    session = qd / "steps" / "p" / "s"
    session.mkdir(parents=True)
    (session / "provider").write_text(provider)
    (session / RESUME_STATE_NAME).write_bytes(encode_native_resume_state(
        NativeResumeState(provider=provider, state={"session_id": "abc"})))
    meta_path = qd / "quest.json"
    meta = json.loads(meta_path.read_text())
    meta["provider"] = provider
    meta["layout_version"] = 22
    meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True))
    return qd


def _meta(quest_dir: Path) -> dict:
    return json.loads((quest_dir / "quest.json").read_text())


def _state(quest_dir: Path) -> dict:
    return json.loads((quest_dir / "state" / "state.json").read_text())


def _events(quest_dir: Path) -> list[dict]:
    return [json.loads(line)
            for line in (quest_dir / "events.jsonl").read_text().splitlines() if line]


def _snapshot(quest_dir: Path) -> dict[str, bytes]:
    return {p.relative_to(quest_dir).as_posix(): p.read_bytes()
            for p in sorted(quest_dir.rglob("*")) if p.is_file()}


def test_all_five_locations_read_the_new_id(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    qd = _seed_v22(tmp_path, "mv23a")

    _migrate_v22_to_v23(qd)

    session = qd / "steps" / "p" / "s"
    assert _meta(qd)["provider"] == "claude"
    assert _state(qd)["resolved_tiers"]["medium"]["provider"] == "claude"
    assert _state(qd)["resolved_steps"]["s"]["provider"] == "claude"
    assert {e["payload"].get("provider") for e in _events(qd)} == {"claude", None}
    assert read_session_owner(session) == "claude"
    assert read_native_resume_state(session) == NativeResumeState(
        provider="claude", state={"session_id": "abc"})
    assert _meta(qd)["layout_version"] == 23


def test_every_event_shape_carrying_a_provider_is_rewritten(tmp_path: Path) -> None:
    # An enumeration of event types would have missed step_started and the two
    # nesting levels under tiers_resolved.
    _seed_control(tmp_path)
    qd = _seed_v22(tmp_path, "mv23b")

    _migrate_v22_to_v23(qd)

    by_type = {e["type"]: e["payload"] for e in _events(qd)}
    assert by_type["step_context"]["provider"] == "claude"
    assert by_type["agent_message"]["provider"] == "claude"
    assert by_type["agent_message"]["raw"]["provider"] == "claude"
    assert by_type["quota_reading"]["provider"] == "claude"
    assert by_type["step_started"]["provider"] == "claude"
    assert by_type["tiers_resolved"]["tiers"]["medium"]["provider"] == "claude"
    assert by_type["tiers_resolved"]["steps"]["s"]["provider"] == "claude"
    assert by_type["step_started"]["round"] == 1
    assert by_type["agent_message"]["raw"]["text"] == "hi"


def test_a_direct_re_invocation_leaves_every_file_byte_identical(
        tmp_path: Path) -> None:
    # migrate() would short-circuit on the version gate and prove nothing; the hop
    # itself must be idempotent so a partial failure re-runs safely.
    _seed_control(tmp_path)
    qd = _seed_v22(tmp_path, "mv23c")
    _migrate_v22_to_v23(qd)
    after_first = _snapshot(qd)

    _migrate_v22_to_v23(qd)

    assert _snapshot(qd) == after_first


def test_a_home_with_no_old_id_is_unchanged_apart_from_the_stamp(
        tmp_path: Path) -> None:
    _seed_control(tmp_path)
    qd = _seed_v22(tmp_path, "mv23d", provider="codex")
    before = _snapshot(qd)

    _migrate_v22_to_v23(qd)

    after = _snapshot(qd)
    assert {k: v for k, v in after.items() if k != "quest.json"} == {
        k: v for k, v in before.items() if k != "quest.json"}
    assert _meta(qd)["provider"] == "codex"
    assert _meta(qd)["layout_version"] == 23


def test_raising_before_the_stamp_leaves_the_home_at_v22(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Work first, stamp last: a stamp-first hop would strand the sidecar at the old id.
    _seed_control(tmp_path)
    qd = _seed_v22(tmp_path, "mv23e")
    session = qd / "steps" / "p" / "s"

    def boom(*a: object, **k: object) -> None:
        raise RuntimeError("crash before the stamp")

    monkeypatch.setattr(quest_mod, "_write_quest_json", boom)
    with pytest.raises(RuntimeError):
        _migrate_v22_to_v23(qd)

    assert read_session_owner(session) == "claude"
    assert _meta(qd)["provider"] == "claude_code"
    assert _meta(qd)["layout_version"] == 22

    monkeypatch.undo()
    _migrate_v22_to_v23(qd)                     # the re-run completes the hop
    assert _meta(qd)["provider"] == "claude"
    assert _meta(qd)["layout_version"] == 23


def test_load_migrates_a_live_home_through_the_chain(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    qd = _seed_v22(tmp_path, "mv23f")

    quest = Quest.load(tmp_path, "mv23f")       # triggers the migration chain

    assert quest.provider == "claude"
    assert read_session_owner(qd / "steps" / "p" / "s") == "claude"
    assert _meta(qd)["layout_version"] == LAYOUT_VERSION


def test_the_hop_is_registered_and_the_constant_moved() -> None:
    assert LAYOUT_VERSION == 23
    assert quest_mod._MIGRATIONS[(22, 23)] is _migrate_v22_to_v23


def _step() -> Step:
    return Step(name="s", phase="p", kind="agent", rules=(), on_max_retries=Pause(),
                max_loops=1, source=SourceLoc(file=Path("workflow.yaml"), line=1),
                file=Path("agents/stub.md"))


def test_a_step_paused_under_the_old_id_keeps_its_session(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    qd = _seed_v22(tmp_path, "mv23g")
    session = qd / "steps" / "p" / "s"
    state_path = qd / "state" / "state.json"
    raw = json.loads(state_path.read_text())
    raw["current_step"] = {"transcript_dir": str(session)}
    state_path.write_text(json.dumps(raw, sort_keys=True))
    with pytest.raises(InvalidResumeState):
        read_native_resume_state(session)   # un-migrated, dispatch would raise outright

    quest = Quest.load(tmp_path, "mv23g")   # triggers the migration chain

    native = read_native_resume_state(session)
    assert native == NativeResumeState(provider="claude", state={"session_id": "abc"})
    state = State.load(quest.state_dir)
    log = quest.event_log()
    assert _retire_foreign_session(
        session, _step(), quest, state, log, is_resume=True, resolved="claude",
        native_resume_state=native, resumable=True) is None
    # The sidecar half stands alone: a session with no manifest is owned by the file.
    assert _retire_foreign_session(
        session, _step(), quest, state, log, is_resume=True, resolved="claude",
        native_resume_state=None, resumable=True) is None
    assert list((qd / "steps" / "p").glob("s.attempt-*")) == []
    assert session.is_dir()
