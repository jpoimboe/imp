"""Interrupt recap: the outermost builder's cut-off set and its `was:` accessor."""
from __future__ import annotations

from typing import Any

import pytest

from imp.cli.narrative import NarrativeBuilder
from tests._event_fixtures import ev

Event = dict[str, Any]

LEAD = "step:implement/task-lead"
THRESHOLD = "step:threshold/threshold"
SPEC_REVIEW = "step:spec/spec-review"
HINT = "interrupted · enter to continue · type to redirect · Ctrl-D to leave"
SCRIPT_HINT = "paused · enter to re-run the gate · Ctrl-D to leave"


def _recap(events: list[Event]) -> str | None:
    b = NarrativeBuilder()
    for e in events:
        b.add(e)
    return b.interrupt_recap()


def _raw(eid: str, etype: str, payload: dict[str, Any]) -> Event:
    # Payloads that carry their own `agent_id` key, which ev() would consume.
    return {"id": eid, "agent_id": None, "type": etype, "payload": payload}


def _task_lead(eid: str = "s1", **extra: Any) -> Event:
    return ev(eid, "step_started", step="task-lead", kind="agent", phase="implement",
              label="task 1/2", position_label=True,
              phase_label="implementation phase · plan 1/2", **extra)


def _threshold() -> Event:
    return ev("s0", "step_started", step="threshold", kind="agent", phase="threshold",
              label=None, show_label=False)


def _dispatch(eid: str, opener: str, name: str, label: str | None) -> Event:
    return ev(eid, "tool_call", opener, id=f"blk-{eid}", name="sub_agent",
              input={"subagent_name": name, "task_description": "do the thing"},
              label=label, id_ns="block")


def _sub_work(dispatch_eid: str) -> Event:
    return ev(f"w-{dispatch_eid}", "thinking", f"sub:{dispatch_eid}", content="working")


def _gate(eid: str, agent: str | None, hint: str = HINT) -> Event:
    return ev(eid, "awaiting_user_input", agent, steer_hint=hint,
              last_agent_message="", interrupt=True)


def _ctrl_c(tag: str, agent: str, *subs: str) -> list[Event]:
    """One Ctrl-C (fuvi18 lines 193-201): each subagent's gate lands before the step agent's."""
    out = [ev(f"{tag}-ir", "interrupt_requested", source="ctrl-c", via="frame")]
    for target in (*subs, agent):
        out += [
            _raw(f"{tag}-ds-{target}", "interrupt_delivery_started",
                 {"agent_id": target, "tier": "cooperative"}),
            ev(f"{tag}-qr-{target}", "query_result", target,
               subtype="error_during_execution", is_error=True),
            _gate(f"{tag}-g-{target}", target),
            _raw(f"{tag}-dd-{target}", "interrupt_delivered",
                 {"agent_id": target, "tier": "cooperative", "outcome": "ok"}),
        ]
    return out


def _interrupted_result(dispatch_eid: str, opener: str, name: str) -> Event:
    return ev(f"r-{dispatch_eid}", "tool_result", opener, id=f"blk-{dispatch_eid}",
              content=(f"RuntimeError: subagent '{name}' was interrupted before finishing"
                       " — the run was paused. [interrupted]"),
              error=True, id_ns="block")


def _leave_and_resume(tag: str) -> list[Event]:
    """Ctrl-D leave, then `imp resume` replays the pause/resume/attach tail (fuvi18 203-205)."""
    return [ev(f"{tag}-wp", "workflow_paused", reason="interrupt", interrupt_reason="ctrl-c"),
            ev(f"{tag}-wr", "workflow_resumed"),
            ev(f"{tag}-ca", "cli_attached")]


# The daemon hold prompt: agent-less, untagged, after the replay boundary.
LIVE_GATE = _gate("live", None)


def _cut_off_then_resume(name: str, label: str | None) -> list[Event]:
    """A task-lead whose one subagent is cut off by Ctrl-C, then left and resumed."""
    return [_task_lead(), _dispatch("d1", LEAD, name, label), _sub_work("d1"),
            *_ctrl_c("a", LEAD, "sub:d1"), _interrupted_result("d1", LEAD, name),
            *_leave_and_resume("a")]


def _two_cut_off() -> list[Event]:
    return [_task_lead(),
            _dispatch("d1", LEAD, "coder", "writing code"),
            _dispatch("d2", LEAD, "code-reviewer", "reviewing code"),
            _sub_work("d1"), _sub_work("d2"),
            *_ctrl_c("a", LEAD, "sub:d1", "sub:d2"),
            _interrupted_result("d1", LEAD, "coder"),
            _interrupted_result("d2", LEAD, "code-reviewer"),
            *_leave_and_resume("a")]


def _lead_cut_off_mid(work: Event) -> list[Event]:
    return [_task_lead(), work, *_ctrl_c("a", LEAD), *_leave_and_resume("a")]


def _spec_review() -> list[Event]:
    return [ev("s1", "step_started", step="spec-review", kind="agent", phase="spec",
               label="reviewing spec"),
            ev("t1", "thinking", SPEC_REVIEW, content="hmm"),
            *_ctrl_c("a", SPEC_REVIEW), *_leave_and_resume("a")]


def _opening_explorer() -> list[Event]:
    return [_threshold(), _dispatch("d1", THRESHOLD, "explorer", "exploring codebase"),
            _sub_work("d1"), *_ctrl_c("a", THRESHOLD, "sub:d1"),
            _interrupted_result("d1", THRESHOLD, "explorer"), *_leave_and_resume("a")]


def _opening_thinking() -> list[Event]:
    return [_threshold(), ev("t1", "thinking", THRESHOLD, content="hmm"),
            *_ctrl_c("a", THRESHOLD), *_leave_and_resume("a")]


@pytest.mark.parametrize(("events", "expected"), [
    pytest.param([*_cut_off_then_resume("coder", "writing code"), LIVE_GATE],
                 "was: task 1/2 · writing code", id="task-coder"),
    pytest.param([*_cut_off_then_resume("code-reviewer", "reviewing code"), LIVE_GATE],
                 "was: task 1/2 · reviewing code", id="task-code-reviewer"),
    pytest.param([*_two_cut_off(), LIVE_GATE],
                 "was: task 1/2 · running 2 subagents", id="task-two-subagents"),
    pytest.param([*_lead_cut_off_mid(ev("t1", "thinking", LEAD, content="hmm")), LIVE_GATE],
                 "was: task 1/2", id="task-mid-thinking"),
    pytest.param([*_lead_cut_off_mid(ev("t1", "tool_call", LEAD, id="tc1", name="bash",
                                        input={"command": "make check"})), LIVE_GATE],
                 "was: task 1/2", id="task-mid-tool"),
    pytest.param([*_spec_review(), LIVE_GATE], "was: reviewing spec", id="spec-review"),
    pytest.param([ev("s1", "step_started", step="verify", kind="script", phase="implement",
                     label="running tests", command="make check"),
                  *_leave_and_resume("a"), _gate("live", None, SCRIPT_HINT)],
                 "was: running tests", id="verify-gate"),
    pytest.param([*_opening_explorer(), LIVE_GATE],
                 "was: exploring codebase", id="opening-explorer"),
    pytest.param([*_opening_thinking(), LIVE_GATE], None, id="opening-nothing-cut-off"),
])
def test_recap_pinned_wordings(events: list[Event], expected: str | None) -> None:
    assert _recap(events) == expected


def test_label_less_fold_falls_back_to_subagent_name() -> None:
    assert (_recap([*_cut_off_then_resume("coder", None), LIVE_GATE])
            == "was: task 1/2 · subagent(coder)")


def test_pause_finalize_records_cut_off_without_gate() -> None:
    events = [_task_lead(), _dispatch("d1", LEAD, "coder", "writing code"), _sub_work("d1"),
              *_leave_and_resume("a"), LIVE_GATE]
    assert _recap(events) == "was: task 1/2 · writing code"


@pytest.mark.parametrize("result", [
    pytest.param({"content": "stopped", "kind": "interrupted"}, id="kind"),
    pytest.param({"content": "RuntimeError: subagent 'coder' failed. [interrupted]"},
                 id="bracket-marker"),
])
def test_interrupted_tool_result_records_cut_off_without_gate(result: dict[str, Any]) -> None:
    events = [_task_lead(), _dispatch("d1", LEAD, "coder", "writing code"), _sub_work("d1"),
              ev("r-d1", "tool_result", LEAD, id="blk-d1", error=True, id_ns="block", **result),
              LIVE_GATE]
    assert _recap(events) == "was: task 1/2 · writing code"


def test_nested_cut_off_is_not_counted() -> None:
    events = [_task_lead(), _dispatch("d1", LEAD, "coder", "writing code"),
              _dispatch("d2", "sub:d1", "explorer", "exploring codebase"), _sub_work("d2"),
              *_ctrl_c("a", LEAD, "sub:d2", "sub:d1"),
              _interrupted_result("d2", "sub:d1", "explorer"),
              _interrupted_result("d1", LEAD, "coder"),
              *_leave_and_resume("a"), LIVE_GATE]
    assert _recap(events) == "was: task 1/2 · writing code"


def test_nested_cut_off_alone_names_no_helper() -> None:
    events = [_task_lead(), _dispatch("d1", LEAD, "coder", "writing code"),
              _dispatch("d2", "sub:d1", "explorer", "exploring codebase"), _sub_work("d2"),
              ev("r-d2", "tool_result", "sub:d1", id="blk-d2", content="stopped",
                 error=True, kind="interrupted", id_ns="block"),
              ev("r-d1", "tool_result", LEAD, id="blk-d1", content="done", id_ns="block"),
              ev("t1", "thinking", LEAD, content="next"),
              *_ctrl_c("a", LEAD), *_leave_and_resume("a"), LIVE_GATE]
    assert _recap(events) == "was: task 1/2"


def test_resumed_step_clears_earlier_cut_off() -> None:
    events = [*_cut_off_then_resume("coder", "writing code"),
              _task_lead("s2", resumed=True, restarted=False),
              ev("t2", "thinking", LEAD, content="checking"),
              *_ctrl_c("b", LEAD), *_leave_and_resume("b"), LIVE_GATE]
    assert _recap(events) == "was: task 1/2"


STEER_REPLY = ev("u1", "user_message", LEAD, role="user",
                 content=[{"type": "text", "text": "try a smaller diff"}], subtype="reply")
ENTER_CONTINUE = ev("u1", "engine_message", LEAD, role="user",
                    content=[{"type": "text", "text": "continue"}], subtype="continuation")


def _coder_cut_off_then(progress: Event) -> list[Event]:
    """Coder cut off at a steer gate; the human answers it in the same step instance."""
    return [_task_lead(), _dispatch("d1", LEAD, "coder", "writing code"), _sub_work("d1"),
            *_ctrl_c("a", LEAD, "sub:d1"), _interrupted_result("d1", LEAD, "coder"),
            progress]


@pytest.mark.parametrize("progress", [
    pytest.param(STEER_REPLY, id="steer-reply"),
    pytest.param(ENTER_CONTINUE, id="enter-continue"),
])
def test_step_agent_message_clears_earlier_cut_off(progress: Event) -> None:
    events = [*_coder_cut_off_then(progress), ev("t2", "thinking", LEAD, content="ok"),
              *_ctrl_c("b", LEAD), *_leave_and_resume("b"), LIVE_GATE]
    assert _recap(events) == "was: task 1/2"


def test_only_the_latest_interrupts_subagents_are_named() -> None:
    events = [*_coder_cut_off_then(STEER_REPLY),
              _dispatch("d2", LEAD, "code-reviewer", "reviewing code"), _sub_work("d2"),
              *_ctrl_c("b", LEAD, "sub:d2"), _interrupted_result("d2", LEAD, "code-reviewer"),
              *_leave_and_resume("b"), LIVE_GATE]
    assert _recap(events) == "was: task 1/2 · reviewing code"


def test_late_result_for_an_already_stopped_fold_does_not_rejoin() -> None:
    events = [_task_lead(), _dispatch("d1", LEAD, "coder", "writing code"), _sub_work("d1"),
              *_ctrl_c("a", LEAD, "sub:d1"), *_leave_and_resume("a"), STEER_REPLY,
              _interrupted_result("d1", LEAD, "coder"), ev("t2", "thinking", LEAD, content="ok"),
              *_ctrl_c("b", LEAD), *_leave_and_resume("b"), LIVE_GATE]
    assert _recap(events) == "was: task 1/2"


def test_ctrl_d_at_resumed_prompt_keeps_helper() -> None:
    events = [*_cut_off_then_resume("coder", "writing code"),
              ev("cd", "interrupt_requested", source="ctrl-d", via="frame"),
              ev("wr2", "workflow_resumed"), ev("ca2", "cli_attached"), LIVE_GATE]
    assert _recap(events) == "was: task 1/2 · writing code"
