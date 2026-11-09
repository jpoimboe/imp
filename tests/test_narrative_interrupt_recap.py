"""Interrupt recap: the outermost builder's cut-off set and its `was:` accessor."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from imp.cli.narrative import AgentTextNode, NarrativeBuilder, ResumeMarkerNode, build_tree
from imp.cli.render_text import render_tree
from tests._event_fixtures import agent_text, ev
from tests.test_narrative_resume_marker import SHARED_DURATIONS

Event = dict[str, Any]

LEAD = "step:implement/task-lead"
THRESHOLD = "step:threshold/threshold"
SPEC_REVIEW = "step:spec/spec-review"
HINT = "interrupted · enter to continue · type to redirect · Ctrl-D to leave"
SCRIPT_HINT = "paused · enter to re-run the gate · Ctrl-D to leave"

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _at(secs: float) -> str:
    return (T0 + timedelta(seconds=secs)).isoformat()


NOW = _at(180)


def _built(events: list[Event]) -> NarrativeBuilder:
    b = NarrativeBuilder()
    for e in events:
        b.add(e)
    return b


def _recap(events: list[Event], now: str = NOW) -> str | None:
    return _built(events).interrupt_recap(now, shown_marker=None)


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


def _ctrl_c(tag: str, agent: str, *subs: str, source: str = "ctrl-c") -> list[Event]:
    """One Ctrl-C (fuvi18 lines 193-201): each subagent's gate lands before the step agent's."""
    out = [ev(f"{tag}-ir", "interrupt_requested", source=source, via="frame")]
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


def _saying(events: list[Event], agent: str, *texts: str) -> list[Event]:
    """`events` with `agent`'s prose turns inserted right after its step_started (events[0])."""
    said = [agent_text(f"said{i}", text, agent_id=agent) for i, text in enumerate(texts)]
    return [events[0], *said, *events[1:]]


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


def _timed(events: list[Event]) -> list[Event]:
    """Stamp every pause at T0 and every resume 1 s later, top-level as in events.jsonl."""
    stamps = {"workflow_paused": _at(0), "workflow_resumed": _at(1)}
    return [{**e, "timestamp": stamps[e["type"]]} if e["type"] in stamps else e
            for e in events]


def _upto_last_gate(events: list[Event]) -> list[Event]:
    """The stream as the client has it when the recap prints: through the last gate."""
    last = max(i for i, e in enumerate(events) if e["type"] == "awaiting_user_input")
    return events[:last + 1]


# AC1
@pytest.mark.parametrize(("events", "expected"), [
    pytest.param(_spec_review(), "was: reviewing spec · paused 3m ago", id="spec-review"),
    pytest.param(_cut_off_then_resume("coder", "writing code"),
                 "was: task 1/2 · writing code · paused 3m ago", id="task-coder"),
    pytest.param(_opening_explorer(), "was: exploring codebase · paused 3m ago",
                 id="opening-explorer"),
    pytest.param(_opening_thinking(), "paused 3m ago", id="opening-nothing-cut-off"),
])
def test_recap_appends_the_pause_age(events: list[Event], expected: str) -> None:
    assert _recap([*_timed(events), LIVE_GATE]) == expected


# AC3(a)
@pytest.mark.parametrize(("gap", "text"), SHARED_DURATIONS)
def test_pause_age_durations_match_the_resume_marker(gap: int, text: str) -> None:
    assert (_recap([*_timed(_spec_review()), LIVE_GATE], now=_at(gap))
            == f"was: reviewing spec · paused {text} ago")


# AC3(c)
@pytest.mark.parametrize("now", [
    pytest.param(_at(0.5), id="half-second"),
    pytest.param(_at(-5), id="clock-skew"),
    pytest.param("not-a-time", id="unparseable"),
])
def test_an_unusable_age_is_omitted(now: str) -> None:
    assert _recap([*_timed(_spec_review()), LIVE_GATE], now=now) == "was: reviewing spec"


def _paused_lead() -> list[Event]:
    """A task-lead mid-thinking, paused at T0, then `imp resume` at T0 + 1 s."""
    return [_task_lead(), ev("t1", "thinking", LEAD, content="hmm"),
            ev("p-wp", "workflow_paused", reason="interrupt", interrupt_reason="ctrl-c",
               timestamp=_at(0)),
            ev("p-wr", "workflow_resumed", timestamp=_at(1))]


LEAD_AGED = "was: task 1/2 · paused 3m ago"


# AC4(a)-(c)
@pytest.mark.parametrize("tail", [
    pytest.param([], id="resume-then-gate"),
    pytest.param([ev("ca", "cli_attached"),
                  ev("cd", "interrupt_requested", source="ctrl-d", via="frame"),
                  ev("wr2", "workflow_resumed", timestamp=_at(100)),
                  ev("ca2", "cli_attached")],
                 id="ctrl-d-at-resumed-prompt-then-resume"),
    pytest.param([ev("ca", "cli_attached"),
                  ev("cd", "interrupt_requested", source="ctrl-d", via="frame")],
                 id="later-attacher-after-another-client-left"),
])
def test_idle_resume_paths_keep_the_pause_age(tail: list[Event]) -> None:
    assert _recap([*_paused_lead(), *tail, LIVE_GATE]) == LEAD_AGED


# AC4(d)
def test_age_measures_from_the_latest_pause() -> None:
    events = [*_paused_lead(), _task_lead("s2", resumed=True, restarted=False),
              ev("t2", "thinking", LEAD, content="checking"),
              ev("p2-wp", "workflow_paused", reason="interrupt", interrupt_reason="ctrl-c",
                 timestamp=_at(100)),
              ev("p2-wr", "workflow_resumed", timestamp=_at(101)), LIVE_GATE]
    assert _recap(events, now=_at(280)) == LEAD_AGED


@pytest.mark.parametrize("between", [
    pytest.param(ev("x", "cli_attached"), id="cli_attached"),
    pytest.param(ev("x", "interrupt_requested", source="ctrl-c", via="frame"), id="ctrl-c"),
    pytest.param(ev("x", "interrupt_requested", source="ctrl-d", via="frame"), id="ctrl-d"),
    pytest.param(ev("x", "interrupt_requested", source="detached", via="frame"),
                 id="detached"),
    pytest.param(_gate("x", LEAD), id="awaiting_user_input"),
])
def test_resume_boundary_bookkeeping_keeps_the_age(between: Event) -> None:
    assert _recap([*_paused_lead(), between, LIVE_GATE]) == LEAD_AGED


@pytest.mark.parametrize("between", [
    pytest.param(_task_lead("x", resumed=True, restarted=False), id="step_started"),
    pytest.param(ev("x", "sandbox_inactive"), id="sandbox_inactive"),
    pytest.param(ev("x", "thinking", LEAD, content="go"), id="thinking"),
    pytest.param(_raw("x", "interrupt_delivery_started",
                      {"agent_id": LEAD, "tier": "cooperative"}),
                 id="interrupt_delivery_started"),
    pytest.param(ev("x", "workflow_resumed", in_process=True, timestamp=_at(2)),
                 id="in-process-resume"),
    pytest.param(ev("x", "workflow_paused", reason="interrupt", interrupt_reason="ctrl-c",
                    timestamp=_at(2)), id="workflow_paused"),
    pytest.param(ev("x", "runner_warning", message="viewer dropped"), id="runner_warning"),
    pytest.param(ev("x", "quest_renamed", old_title="a", new_title="b"), id="quest_renamed"),
])
def test_any_other_event_disarms_the_age(between: Event) -> None:
    assert _recap([*_paused_lead(), between, LIVE_GATE]) == "was: task 1/2"


def test_an_in_process_resume_does_not_arm_the_age() -> None:
    events = [_task_lead(), ev("t1", "thinking", LEAD, content="hmm"),
              ev("wp", "workflow_paused", reason="interrupt", interrupt_reason="ctrl-c",
                 timestamp=_at(0)),
              ev("wr", "workflow_resumed", in_process=True, timestamp=_at(1)), LIVE_GATE]
    assert _recap(events) == "was: task 1/2"


def _resumed_then_ran() -> list[Event]:
    """Pause at T0, `imp resume`, then the task-lead runs again before any gate."""
    return [*_paused_lead(), ev("ca", "cli_attached"),
            _task_lead("s2", resumed=True, restarted=False),
            ev("t2", "thinking", LEAD, content="checking")]


def _ran_then_run_stopped() -> list[Event]:
    return [*_resumed_then_ran(),
            ev("b-ir", "interrupt_requested", source="ctrl-c", via="frame"),
            ev("b-wp", "workflow_paused", reason="interrupt", interrupt_reason="ctrl-c",
               timestamp=_at(120)),
            LIVE_GATE]


def _ran_then_in_session_pause() -> list[Event]:
    return _upto_last_gate([
        *_resumed_then_ran(),
        ev("b-wp", "workflow_paused", reason="interrupt", interrupt_reason="pause",
           timestamp=_at(120)),
        ev("b-wr", "workflow_resumed", in_process=True, timestamp=_at(130)),
        _task_lead("s3", resumed=True, restarted=False),
        ev("t3", "thinking", LEAD, content="again"), *_ctrl_c("c", LEAD)])


# AC5(a)-(d)
@pytest.mark.parametrize("events", [
    pytest.param(_upto_last_gate([*_resumed_then_ran(), *_ctrl_c("b", LEAD)]),
                 id="live-ctrl-c"),
    pytest.param(_upto_last_gate([*_resumed_then_ran(),
                                  *_ctrl_c("b", LEAD, source="ctrl-d")]),
                 id="other-client-left"),
    pytest.param(_ran_then_run_stopped(), id="run-stopping-interrupt"),
    pytest.param(_ran_then_in_session_pause(), id="in-session-pause"),
])
def test_executed_after_resume_paths_show_no_age(events: list[Event]) -> None:
    assert _recap(events, now=_at(600)) == "was: task 1/2"


_LOG_PREFIX = [
    "", "── implementation phase · plan 1/2 ───", "",
    "implementation phase · plan 1/2 · task 1/2", "", "▾ thinking", "  hmm", "",
    "paused · resumed 1s later", "", "implementation phase · plan 1/2 · task 1/2 (resume)", "",
    "▾ thinking", "  checking", ""]


# AC5(g): golden from before the anchor existed; full mode shows unregistered interrupt_* rows.
@pytest.mark.parametrize(("events", "expected"), [
    pytest.param(_ran_then_run_stopped(),
                 [*_LOG_PREFIX, "interrupt_requested  source=ctrl-c via=frame", "",
                  "paused · Ctrl-C"],
                 id="run-stopping-interrupt"),
    pytest.param(_ran_then_in_session_pause(),
                 [*_LOG_PREFIX, "implementation phase · plan 1/2 · task 1/2 (resume)", "",
                  "▾ thinking", "  again", "",
                  "interrupt_requested  source=ctrl-c via=frame",
                  "interrupt_delivery_started  agent_id=step:implement/task-lead"
                  " tier=cooperative"],
                 id="in-session-pause"),
])
def test_imp_log_rendering_ignores_the_anchor(events: list[Event], expected: list[str]) -> None:
    assert list(render_tree(build_tree(events), full=True, color=False, width=80)) == expected


def _message(events: list[Event]) -> str | None:
    b = NarrativeBuilder()
    for e in events:
        b.add(e)
    found = b.latest_step_message_at()
    if found is None:
        return None
    step, index = found
    node = step.children[index]
    assert isinstance(node, AgentTextNode)
    return node.text


def _coder_said() -> list[Event]:
    """The single-cut-off coder scenario where only the coder (a subagent) wrote prose."""
    return [_task_lead(), _dispatch("d1", LEAD, "coder", "writing code"),
            agent_text("sp", "Coder prose.", agent_id="sub:d1"),
            *_ctrl_c("a", LEAD, "sub:d1"), _interrupted_result("d1", LEAD, "coder"),
            *_leave_and_resume("a")]


# AC6
@pytest.mark.parametrize(("events", "expected"), [
    pytest.param([*_saying(_spec_review(), SPEC_REVIEW, "Reading the spec.",
                           "Checking criterion 4."), LIVE_GATE],
                 "Checking criterion 4.", id="latest-of-several"),
    pytest.param([*_lead_cut_off_mid(ev("t1", "thinking", LEAD, content="hmm")), LIVE_GATE],
                 None, id="thinking-only"),
    pytest.param([*_lead_cut_off_mid(ev("t1", "tool_call", LEAD, id="tc1", name="bash",
                                        input={"command": "make check"})), LIVE_GATE],
                 None, id="tool-only"),
    pytest.param([*_coder_said(), LIVE_GATE], None, id="subagent-prose-only"),
    pytest.param([*_saying(_coder_said(), LEAD, "Dispatching the coder."), LIVE_GATE],
                 "Dispatching the coder.", id="subagent-prose-ignored"),
    pytest.param([], None, id="no-step"),
])
def test_latest_step_message(events: list[Event], expected: str | None) -> None:
    assert _message(events) == expected


def _resume_markers(b: NarrativeBuilder) -> list[ResumeMarkerNode]:
    found: list[ResumeMarkerNode] = []
    stack: list[Any] = [b.tree.root]
    while stack:
        node = stack.pop()
        if isinstance(node, ResumeMarkerNode):
            found.append(node)
        stack.extend(getattr(node, "children", ()))
    return found


def _spec_review_resumed(pause: dict[str, Any], resumed_at: float = 1) -> list[Event]:
    """`_spec_review` left by a `pause` stamped T0, then resumed `resumed_at` s later."""
    return [*_spec_review()[:2], *_ctrl_c("a", SPEC_REVIEW),
            ev("a-wp", "workflow_paused", timestamp=_at(0), **pause),
            ev("a-wr", "workflow_resumed", timestamp=_at(resumed_at)),
            ev("a-ca", "cli_attached")]


SPEC_AGED = "was: reviewing spec · paused 3m ago"


@pytest.mark.parametrize(("pause", "resumed_at"), [
    pytest.param({"reason": "interrupt"}, 1, id="plain-interrupt"),
    pytest.param({"reason": "compact"}, 1, id="compact"),
    pytest.param({"reason": "interrupt", "interrupt_reason": "ctrl-c"}, 0.5,
                 id="sub-second-gap"),
])
def test_a_resume_without_a_marker_keeps_the_age(
    pause: dict[str, Any], resumed_at: float,
) -> None:
    b = _built([*_spec_review_resumed(pause, resumed_at), LIVE_GATE])
    assert _resume_markers(b) == []
    assert b.interrupt_recap(NOW, shown_marker=None) == SPEC_AGED


@pytest.mark.parametrize(("events", "expected"), [
    pytest.param(_spec_review(), "was: reviewing spec", id="labelled"),
    pytest.param(_opening_thinking(), None, id="age-only-recap-empties"),
])
def test_showing_this_resumes_marker_drops_the_age(
    events: list[Event], expected: str | None,
) -> None:
    b = _built([*_timed(events), LIVE_GATE])
    [marker] = _resume_markers(b)
    assert b.interrupt_recap(NOW, shown_marker=marker) == expected


def test_an_equal_but_distinct_marker_keeps_the_age() -> None:
    b = _built([*_timed(_spec_review()), LIVE_GATE])
    [marker] = _resume_markers(b)
    twin = ResumeMarkerNode(gap_s=marker.gap_s)
    assert twin == marker and twin is not marker
    assert b.interrupt_recap(NOW, shown_marker=twin) == SPEC_AGED


def test_a_rearmed_anchor_forgets_the_earlier_marker() -> None:
    b = _built([*_timed(_spec_review()),
                ev("c-wp", "workflow_paused", reason="compact", timestamp=_at(100)),
                ev("c-wr", "workflow_resumed", timestamp=_at(101)),
                ev("c-ca", "cli_attached"), LIVE_GATE])
    [m1] = _resume_markers(b)
    assert b.interrupt_recap(_at(280), shown_marker=m1) == SPEC_AGED
