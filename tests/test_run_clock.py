"""The run clock counts only while a run is live. Fixtures copy the sequences and
gaps captured in spike muma80 (event_sequences.md)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from imp.cli import narrative as nv
from imp.cli.render_summary import (
    build_summary,
    exclusion_spans,
    summary_total,
    summary_totals,
)

_T0 = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
_AID = "step:spec/spec-writer"
_STEP: dict[str, Any] = {"step": "spec-writer", "phase": "spec", "kind": "agent"}


class _Log:
    """Events at cumulative offsets: each `add` lands `delta` seconds after the last."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self._t = 0.0

    def add(self, delta: float, etype: str, agent_id: str | None = None,
            **payload: Any) -> _Log:
        self._t = round(self._t + delta, 1)
        self.events.append({
            "id": f"e{len(self.events)}", "type": etype, "agent_id": agent_id,
            "timestamp": (_T0 + timedelta(seconds=self._t)).isoformat(),
            "payload": payload})
        return self

    def snapshot(self) -> list[dict[str, Any]]:
        return list(self.events)


def _work(events: list[dict[str, Any]]) -> float | None:
    work, _ = summary_totals(build_summary(events), summary_total(events),
                             exclusion_spans(events))
    return work


def _first_run(log: _Log, gap: float) -> _Log:
    """riga96 idx 0-9: quest_created, the dormant gap, the first run. 5.4s of work."""
    return (log.add(0.0, "quest_created", title="x")
            .add(gap, "tiers_resolved")
            .add(0.0, "user_message", role="user", subtype="intent", content=[])
            .add(0.0, "step_started", **_STEP, resumed=False, restarted=False)
            .add(0.3, "token_refresh", _AID, outcome="skipped_fresh")
            .add(0.0, "step_context", _AID)
            .add(0.0, "engine_message", _AID, subtype="system_prompt", role="system")
            .add(0.0, "engine_message", _AID, subtype="agent_prompt", role="user")
            .add(0.0, "cli_attached")
            .add(5.1, "agent_message", _AID, provider="anthropic", model="m",
                 content=[{"type": "text", "text": "hi"}]))


def _ask(log: _Log) -> _Log:
    """sedu45 idx 13-16: a model turn ending at a question. 13.5s of work."""
    return (log.add(5.1, "thinking", _AID, content="hmm")
            .add(8.4, "agent_message", _AID, provider="anthropic", model="m",
                 content=[{"type": "text", "text": "which one?"}])
            .add(0.0, "query_result", _AID, subtype="success")
            .add(0.0, "awaiting_user_input", _AID, interrupt=False))


def _interrupt_at_a_wait(log: _Log) -> _Log:
    """pore03 idx 423-427: a detach mid-turn. 4.9s of work, then the wait and the pause."""
    return (log.add(4.9, "interrupt_requested", source="detached")
            .add(0.0, "query_result", _AID, subtype="error_during_execution")
            .add(0.0, "awaiting_user_input", _AID, interrupt=True)
            .add(0.0, "interrupt_delivered", outcome="ok")
            .add(0.4, "workflow_paused", reason="interrupt", interrupt_reason="detached"))


def _resume_at_a_wait(log: _Log, gap: float, *, refreshed: bool = False) -> _Log:
    """sedu45 idx 18-22 (kora58 adds workflow_refreshed): the wait re-armed, no model turn."""
    log.add(gap, "workflow_resumed")
    if refreshed:
        log.add(0.0, "workflow_refreshed")
    return (log.add(0.1, "step_started", **_STEP, resumed=True, restarted=False)
            .add(0.3, "token_refresh", _AID, outcome="skipped_fresh")
            .add(0.0, "awaiting_user_input", _AID, interrupt=False)
            .add(0.0, "cli_attached"))


def _crash_then_resume(log: _Log, *, attached_first: bool) -> _Log:
    """No pause event: 2.5s of work, a 2h crash gap, a continuation. 6.0s of work after."""
    log.add(2.0, "tool_call", _AID, name="read_file", id="t1", input={})
    log.add(0.5, "tool_result", _AID, id="t1")
    if attached_first:
        log.add(7200.0, "cli_attached").add(0.6, "workflow_resumed")
    else:
        log.add(7200.6, "workflow_resumed")
    log.add(0.1, "step_started", **_STEP, resumed=True, restarted=False)
    log.add(0.5, "token_refresh", _AID, outcome="skipped_fresh")
    log.add(0.0, "engine_message", _AID, subtype="continuation", role="user")
    if not attached_first:
        log.add(0.0, "cli_attached")
    log.add(3.4, "thinking", _AID, content="back")
    return log.add(2.0, "agent_message", _AID, provider="anthropic", model="m",
                   content=[{"type": "text", "text": "done"}])


def _saved_draft_then_rerun(log: _Log) -> _Log:
    """kora58 idx 451-460: left mid-turn (conversation_saved only), re-run later."""
    return (log.add(9.5, "thinking", _AID, content="hmm")
            .add(4.6, "conversation_saved")
            .add(339.5, "workflow_resumed")
            .add(0.0, "workflow_refreshed")
            .add(0.0, "step_started", **_STEP, resumed=True, restarted=False)
            .add(0.4, "token_refresh", _AID, outcome="skipped_fresh")
            .add(0.0, "awaiting_user_input", _AID, interrupt=False)
            .add(0.0, "cli_attached")
            .add(9.2, "user_message", _AID, role="user", subtype="reply", content=[])
            .add(7.4, "thinking", _AID, content="ok"))


def _hold_prompt(log: _Log) -> _Log:
    """pore03 idx 428-434: resume, an unlogged 96s hold prompt, an in-process resume."""
    return (log.add(7.6, "workflow_resumed")
            .add(0.0, "cli_attached")
            .add(96.2, "workflow_resumed")
            .add(0.1, "step_started", **_STEP, resumed=True, restarted=False)
            .add(0.4, "token_refresh", _AID, outcome="skipped_fresh")
            .add(0.0, "user_message", _AID, role="user", subtype="interject", content=[])
            .add(47.2, "thinking", _AID, content="ok"))


def _late_interrupt_then_reply(log: _Log) -> _Log:
    """dere68 idx 166-176: 2.0s of work, a pause with late interrupt events, a reply."""
    return (log.add(2.0, "thinking", _AID, content="hmm")
            .add(0.0, "error", _AID, kind="interrupted", category="config")
            .add(0.0, "workflow_paused", reason="interrupt", interrupt_reason="ctrl-c")
            .add(2.0, "interrupt_delivered", outcome="timeout")
            .add(0.0, "interrupt_escalated", reason="cooperative_timeout")
            .add(0.0, "interrupt_delivered", outcome="ok")
            .add(23.3, "workflow_resumed")
            .add(0.1, "step_started", **_STEP, resumed=True, restarted=False)
            .add(0.0, "token_refresh", _AID, outcome="skipped_fresh")
            .add(0.0, "user_message", _AID, role="user", subtype="reply", content=[])
            .add(5.3, "thinking", _AID, content="ok"))


def _done(log: _Log) -> _Log:
    return log.add(1.0, "step_completed", step="spec-writer", kind="agent",
                   action={"verb": "continue"}, var_mutations={})


def test_open_pause_and_late_rename_then_a_no_work_resume_keep_the_total() -> None:
    log = _interrupt_at_a_wait(_first_run(_Log(), 1.3))
    assert _work(log.snapshot()) == pytest.approx(10.3)
    log.add(3600.0, "quest_renamed", new_title="renamed while paused")
    before = _work(log.snapshot())
    _resume_at_a_wait(log, 6395.7).add(13968.1, "workflow_paused", reason="awaiting_user")
    assert before == pytest.approx(10.3)
    assert _work(log.events) == pytest.approx(10.3)


def test_backlog_dormancy_before_the_first_run_is_excluded() -> None:
    log = _ask(_first_run(_Log(), 18 * 3600.0))
    assert _work(log.events) == pytest.approx(18.9)


@pytest.mark.parametrize("attached_first", [True, False],
                         ids=["attach-then-resume", "resume-then-attach"])
def test_a_crash_gap_before_a_resume_is_excluded(attached_first: bool) -> None:
    log = _first_run(_Log(), 1.3)
    _crash_then_resume(log, attached_first=attached_first)
    assert _work(log.events) == pytest.approx(5.4 + 2.5 + 6.0)


def test_a_saved_drafts_gap_before_the_re_run_is_excluded() -> None:
    log = _first_run(_Log(), 1.3)
    log.add(9.5, "thinking", _AID, content="hmm").add(4.6, "conversation_saved")
    assert _work(log.snapshot()) == pytest.approx(14.9)
    full = _saved_draft_then_rerun(_first_run(_Log(), 1.3))
    assert _work(full.events) == pytest.approx(14.9 + 7.4)


@pytest.mark.parametrize("tail", ["quest_abandoned", "conversation_archived", "quest_renamed"])
@pytest.mark.parametrize("at_wait", [True, False], ids=["at-a-wait", "mid-turn"])
def test_trailing_lifecycle_events_days_later_leave_the_total(tail: str, at_wait: bool) -> None:
    log = _first_run(_Log(), 1.3)
    if at_wait:
        _ask(log)
    expected = 18.9 if at_wait else 5.4
    assert _work(log.snapshot()) == pytest.approx(expected)
    log.add(3 * 86400.0, tail, **({"new_title": "later"} if tail == "quest_renamed" else {}))
    assert _work(log.events) == pytest.approx(expected)


@pytest.mark.parametrize("refreshed", [False, True], ids=["plain", "workflow_refreshed"])
def test_a_resume_at_a_wait_with_no_model_turn_keeps_the_total(refreshed: bool) -> None:
    log = _ask(_first_run(_Log(), 1.3))
    log.add(5901.7, "workflow_paused", reason="awaiting_user")
    before = _work(log.snapshot())
    _resume_at_a_wait(log, 6395.7, refreshed=refreshed)
    log.add(13968.1, "workflow_paused", reason="awaiting_user")
    assert before == pytest.approx(18.9)
    assert _work(log.events) == pytest.approx(18.9)


def test_an_unlogged_hold_prompt_between_two_resumes_is_excluded() -> None:
    log = _hold_prompt(_interrupt_at_a_wait(_first_run(_Log(), 1.3)))
    assert _work(log.events) == pytest.approx(10.3 + 0.4 + 47.2)


def test_late_interrupt_events_after_a_pause_add_no_time() -> None:
    log = _first_run(_Log(), 1.3)
    log.add(2.0, "thinking", _AID, content="hmm")
    log.add(0.0, "error", _AID, kind="interrupted", category="config")
    log.add(0.0, "workflow_paused", reason="interrupt", interrupt_reason="ctrl-c")
    assert _work(log.snapshot()) == pytest.approx(7.4)
    log.add(2.0, "interrupt_delivered", outcome="timeout")
    log.add(0.0, "interrupt_escalated", reason="cooperative_timeout")
    log.add(0.0, "interrupt_delivered", outcome="ok")
    assert _work(log.snapshot()) == pytest.approx(7.4)
    full = _late_interrupt_then_reply(_first_run(_Log(), 1.3))
    assert _work(full.events) == pytest.approx(7.4 + 0.1 + 5.3)


@pytest.mark.parametrize("leave", ["conversation_saved", "conversation_archived"])
def test_a_leave_at_a_wait_without_a_pause_event_excludes_the_wait(leave: str) -> None:
    log = _ask(_first_run(_Log(), 1.3))
    log.add(551.9, leave, interrupt_reason="ctrl-d")
    assert _work(log.events) == pytest.approx(18.9)


def test_a_reply_after_a_saved_wait_counts_only_the_new_work() -> None:
    """hava67 idx 201-209."""
    log = _ask(_first_run(_Log(), 1.3))
    log.add(551.9, "conversation_saved", interrupt_reason="ctrl-d")
    _resume_at_a_wait(log, 428.1)
    log.add(0.0, "user_message", _AID, role="user", subtype="reply", content=[])
    log.add(43.4, "thinking", _AID, content="ok")
    assert _work(log.events) == pytest.approx(18.9 + 43.4)


def _open_pause_renamed_then_reply() -> _Log:
    log = _interrupt_at_a_wait(_first_run(_Log(), 1.3))
    log.add(3600.0, "quest_renamed", new_title="renamed while paused")
    _resume_at_a_wait(log, 6395.7)
    log.add(5.0, "user_message", _AID, role="user", subtype="reply", content=[])
    return _done(log.add(2.0, "thinking", _AID, content="ok"))


def _paused_at_a_wait_then_reply() -> _Log:
    log = _ask(_first_run(_Log(), 1.3))
    log.add(5901.7, "workflow_paused", reason="awaiting_user")
    _resume_at_a_wait(log, 6395.7, refreshed=True)
    log.add(13.0, "user_message", _AID, role="user", subtype="reply", content=[])
    return _done(log.add(6.0, "thinking", _AID, content="ok"))


def _backlog_then_archived() -> _Log:
    log = _ask(_first_run(_Log(), 18 * 3600.0))
    log.add(20.0, "user_message", _AID, role="user", subtype="reply", content=[])
    _done(log.add(3.0, "thinking", _AID, content="ok"))
    return log.add(3 * 86400.0, "conversation_archived")


def _saved_wait_then_reply() -> _Log:
    log = _ask(_first_run(_Log(), 1.3))
    log.add(551.9, "conversation_saved", interrupt_reason="ctrl-d")
    _resume_at_a_wait(log, 428.1)
    log.add(0.0, "user_message", _AID, role="user", subtype="reply", content=[])
    return _done(log.add(43.4, "thinking", _AID, content="ok"))


@pytest.mark.parametrize("build,expected", [
    (_open_pause_renamed_then_reply, 10.3 + 2.0 + 1.0),
    (_backlog_then_archived, 18.9 + 3.0 + 1.0),
    (lambda: _done(_crash_then_resume(_first_run(_Log(), 1.3), attached_first=True)),
     5.4 + 2.5 + 6.0 + 1.0),
    (lambda: _done(_crash_then_resume(_first_run(_Log(), 1.3), attached_first=False)),
     5.4 + 2.5 + 6.0 + 1.0),
    (lambda: _done(_saved_draft_then_rerun(_first_run(_Log(), 1.3))), 14.9 + 7.4 + 1.0),
    (_paused_at_a_wait_then_reply, 18.9 + 6.0 + 1.0),
    (lambda: _done(_hold_prompt(_interrupt_at_a_wait(_first_run(_Log(), 1.3)))),
     10.3 + 0.4 + 47.2 + 1.0),
    (lambda: _done(_late_interrupt_then_reply(_first_run(_Log(), 1.3))),
     7.4 + 0.1 + 5.3 + 1.0),
    (_saved_wait_then_reply, 18.9 + 43.4 + 1.0),
], ids=["open-pause-rename", "backlog-archived", "crash-attach-first", "crash-attach-after",
        "saved-draft", "resume-at-a-wait", "hold-prompt", "late-interrupt", "saved-wait"])
def test_step_and_phase_times_follow_the_run_clock(build: Any, expected: float) -> None:
    events = build().events
    tree = nv.build_tree(events)
    phases = [p for p in tree.root.children if isinstance(p, nv.PhaseNode)]
    crumbs = [c.elapsed_s for p in phases for s in p.children if isinstance(s, nv.StepNode)
              for c in s.children if isinstance(c, nv.BreadcrumbLine)]
    assert [r.work_seconds for r in build_summary(events)] == [pytest.approx(expected)]
    assert crumbs == [pytest.approx(expected)]
    assert [nv.phase_elapsed(p) for p in phases] == [pytest.approx(expected)]
    assert _work(events) == pytest.approx(expected)


def test_a_merge_lane_wait_inside_a_live_run_is_counted() -> None:
    log = _done(_first_run(_Log(), 1.3))
    log.add(0.1, "step_started", step="merge", phase="merge", kind="merge")
    log.add(0.0, "lane_waiting", base="main", ahead=1)
    log.add(300.0, "lane_acquired", base="main", step="merge")
    log.add(2.0, "step_completed", step="merge", kind="merge",
            action={"verb": "continue"}, var_mutations={})
    assert _work(log.events) == pytest.approx(5.4 + 1.0 + 0.1 + 300.0 + 2.0)


def test_a_dependency_wait_straight_after_a_resume_is_counted() -> None:
    log = _first_run(_Log(), 1.3)
    log.add(2.0, "thinking", _AID, content="hmm")
    log.add(0.0, "workflow_paused", reason="interrupt", interrupt_reason="ctrl-c")
    log.add(60.0, "workflow_resumed")
    log.add(0.1, "step_started", **_STEP, resumed=True, restarted=False)
    log.add(0.0, "token_refresh", _AID, outcome="skipped_fresh")
    log.add(0.0, "dependency_waiting", _AID, depends_on="abcd12")
    log.add(120.0, "dependency_ready", _AID)
    log.add(3.0, "thinking", _AID, content="unblocked")
    assert _work(log.events) == pytest.approx(7.4 + 0.1 + 120.0 + 3.0)


def test_a_completed_step_ends_the_startup_before_a_later_step_re_arms_a_wait() -> None:
    log = _first_run(_Log(), 1.3)
    log.add(2.0, "thinking", _AID, content="hmm")
    log.add(0.0, "workflow_paused", reason="interrupt", interrupt_reason="ctrl-c")
    log.add(60.0, "workflow_resumed")
    log.add(0.1, "step_started", **_STEP, resumed=True, restarted=False)
    log.add(30.0, "step_completed", step="spec-writer", kind="agent",
            action={"verb": "continue"}, var_mutations={})
    log.add(0.1, "step_started", step="review", phase="spec", kind="agent",
            resumed=False, restarted=False)
    log.add(0.0, "awaiting_user_input", "step:spec/review", interrupt=False)
    assert _work(log.events) == pytest.approx(5.4 + 2.0 + 0.1 + 30.0 + 0.1)
