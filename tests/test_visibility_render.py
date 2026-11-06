from __future__ import annotations

import subprocess
import sys

from imp.cli import narrative as nv
from imp.cli.narrative import deferred_leaf, step_hidden, step_no_body
from imp.cli.render_text import frame_suppressed, render_tree
from tests._event_fixtures import ev


def _body(events) -> list[str]:
    return list(render_tree(nv.build_tree(events), full=False, color=False, width=80))


def test_show_label_false_script_silent_on_linear_continue() -> None:
    body = _body([
        ev("1", "quest_created", quest_id=1, title="x"),
        ev("2", "step_started", step="cleanup", kind="script", phase="implement",
           show_label=False),
        ev("3", "step_completed", step="cleanup", kind="script",
           action={"verb": "continue"}, var_mutations={}),
    ])
    assert not any("cleanup" in ln for ln in body)


def test_show_label_false_script_reveals_frame_and_error_on_pause() -> None:
    body = _body([
        ev("1", "quest_created", quest_id=1, title="x"),
        ev("2", "step_started", step="merge-guard", kind="script", phase="merge",
           show_label=False),
        ev("3", "error", message="boom in bar.py"),
        ev("4", "step_completed", step="merge-guard", kind="script",
           action={"verb": "pause"}, var_mutations={}),
    ])
    assert any("merge-guard" in ln for ln in body)    # frame returns on pause
    assert any("boom in bar.py" in ln for ln in body)  # error not swallowed


def test_show_label_false_agent_shows_prose_without_step_header() -> None:
    body = _body([
        ev("1", "quest_created", quest_id=1, title="x"),
        ev("2", "step_started", step="scoping", kind="agent", phase="threshold",
           model="m", provider="anthropic", label="chatting", show_label=False),
        ev("3", "agent_message", agent_id="step:threshold/scoping",
           provider="anthropic", model="m", stop_reason="end_turn", usage={},
           raw={"content": [{"type": "text", "text": "how can I help"}]}),
        ev("4", "step_completed", step="scoping", kind="agent",
           action={"verb": "continue"}, var_mutations={}),
    ])
    out = "\n".join(body)
    assert "how can I help" in out    # conversation body survives
    assert "chatting ·" not in out    # step header/label suppressed


# --- predicate unit tests ---

def test_visible_body_less_script_is_collapsible_leaf() -> None:
    node = nv.StepNode(name="s", kind="script", show_label=True)
    assert step_hidden(node) is False
    assert deferred_leaf(node) is True                 # body-less → collapsible



def test_show_label_false_script_no_output_is_deferred_leaf() -> None:
    node = nv.StepNode(name="s", kind="script", show_label=False, show_output=False)
    assert step_hidden(node) is True
    assert step_no_body(node) is True
    assert deferred_leaf(node) is True


def test_agent_step_no_body_false_so_not_deferred() -> None:
    # Agents always render prose; step_no_body is False regardless of show_output.
    node = nv.StepNode(name="s", kind="agent", show_label=False, show_output=False)
    assert step_no_body(node) is False
    assert deferred_leaf(node) is False


def test_show_label_false_with_show_output_not_deferred_but_frame_suppressed_on_continue() -> None:
    node = nv.StepNode(name="s", kind="script", show_label=False, show_output=True)
    assert step_no_body(node) is False
    assert deferred_leaf(node) is False
    # Frame is still suppressed when the outcome is a linear continue.
    node.outcome = nv.OutcomeLine(action={"verb": "continue"})
    assert frame_suppressed(node) is True


def test_frame_suppressed_false_on_pause_outcome() -> None:
    node = nv.StepNode(name="s", kind="script", show_label=False, show_output=True)
    node.outcome = nv.OutcomeLine(action={"verb": "pause"})
    assert frame_suppressed(node) is False


def test_script_blocked_reveal_is_length_bounded() -> None:
    big = "L" + ("x" * 6000) + "R"
    events = [
        ev("1", "quest_created", quest_id=1, title="x"),
        ev("2", "step_started", step="verify", kind="script", phase="implement",
           show_label=False),
        ev("3", "script_blocked", step="verify", exit=1, stdout="", stderr=big),
        ev("4", "step_completed", step="verify", kind="script",
           action={"verb": "pause"}, var_mutations={}),
    ]
    body = "\n".join(render_tree(nv.build_tree(events), full=False, color=False, width=80))
    assert "lines truncated" in body     # _truncate head+tail marker present


def test_script_output_renders_bullet_message_under_step() -> None:
    events = [
        ev("1", "quest_created", quest_id=1, title="x"),
        ev("2", "step_started", step="announce", kind="script", phase="plan",
           show_output=True, label="finalizing"),
        ev("3", "script_output", step="announce", stdout="plan 2/3 · 4 tasks"),
        ev("4", "step_completed", step="announce", kind="script",
           action={"verb": "continue"}, var_mutations={}),
    ]
    tree = nv.build_tree(events)
    lits = [c for ph in tree.root.children if isinstance(ph, nv.PhaseNode)
            for st in ph.children if isinstance(st, nv.StepNode)
            for c in st.children if isinstance(c, nv.LiteralTextNode)]
    assert any("plan 2/3 · 4 tasks" in n.text and n.verbatim is False for n in lits)
    body = "\n".join(render_tree(tree, full=False, color=False, width=80))
    assert "plan 2/3 · 4 tasks" in body


def test_phase_show_label_false_suppresses_phase_header() -> None:
    body = _body([
        ev("1", "quest_created", quest_id=1, title="x"),
        ev("2", "step_started", step="threshold", kind="agent",
           phase="threshold", model="m", provider="anthropic",
           label="chatting", phase_label="threshold phase",
           show_label=False, phase_show_label=False),
        ev("3", "agent_message", agent_id="step:threshold/threshold",
           provider="anthropic", model="m", stop_reason="end_turn", usage={},
           raw={"content": [{"type": "text", "text": "hello there"}]}),
        ev("4", "step_completed", step="threshold", kind="agent",
           action={"verb": "continue"}, var_mutations={}),
    ])
    out = "\n".join(body)
    assert "hello there" in out                 # conversation body survives
    assert "threshold phase" not in out        # phase header suppressed


def test_frame_suppressed_opening_collapses_stacked_blanks() -> None:
    # A log that opens on a frame-suppressed threshold phase+step must show exactly
    # ONE blank between the opening human turn and the first agent words — matching
    # the live viewer — not the three stacked blanks the un-guarded batch walk
    # accumulated from spaces_before(Phase) + the phase prime + the step prime.
    body = _body([
        ev("1", "quest_created", quest_id=1, title="x"),
        ev("2", "user_message", subtype="intent",
           content=[{"type": "text", "text": "do the thing"}]),
        ev("3", "step_started", step="threshold", kind="agent", phase="threshold",
           model="m", provider="anthropic", label="chatting",
           phase_label="threshold phase", show_label=False, phase_show_label=False),
        ev("4", "agent_message", agent_id="step:threshold/threshold",
           provider="anthropic", model="m", stop_reason="end_turn", usage={},
           raw={"content": [{"type": "text", "text": "I can help with that"}]}),
        ev("5", "step_completed", step="threshold", kind="agent",
           action={"verb": "continue"}, var_mutations={}),
    ])
    assert body == ["> do the thing", "", "• I can help with that"]


def test_frame_suppressed_phase_opens_transcript_on_content() -> None:
    # With no preceding prose, a suppressed opening phase must NOT assert its
    # spaces_before blank: the transcript opens directly on the first agent words,
    # with no leading blank run at the top.
    body = _body([
        ev("1", "quest_created", quest_id=1, title="x"),
        ev("2", "step_started", step="threshold", kind="agent", phase="threshold",
           model="m", provider="anthropic", label="chatting",
           phase_label="threshold phase", show_label=False, phase_show_label=False),
        ev("3", "agent_message", agent_id="step:threshold/threshold",
           provider="anthropic", model="m", stop_reason="end_turn", usage={},
           raw={"content": [{"type": "text", "text": "first agent words"}]}),
        ev("4", "step_completed", step="threshold", kind="agent",
           action={"verb": "continue"}, var_mutations={}),
    ])
    assert body == ["• first agent words"]


def test_visible_phase_keeps_lead_in_blank_below_header() -> None:
    # Regression: a VISIBLE phase still primes its children walk, so a single blank
    # separates the phase rule from the first step header. This guards the
    # prime=not suppress change against stripping the lead-in blank of a loud frame.
    body = _body([
        ev("1", "quest_created", quest_id=1, title="x"),
        ev("2", "step_started", step="spec", kind="agent", phase="design",
           model="m", provider="anthropic", label="drafting",
           phase_label="design phase", show_label=True, phase_show_label=True),
        ev("3", "agent_message", agent_id="step:design/spec",
           provider="anthropic", model="m", stop_reason="end_turn", usage={},
           raw={"content": [{"type": "text", "text": "the plan"}]}),
        ev("4", "step_completed", step="spec", kind="agent",
           action={"verb": "continue"}, var_mutations={}),
    ])
    rule = body.index("── design phase ───")
    assert body[rule + 1] == ""            # lead-in blank below the phase rule
    assert "drafting" in body[rule + 2]    # step header follows that single blank


def test_importing_narrative_alone_loads_no_other_cli_module() -> None:
    code = ("import sys, imp.cli.narrative; "
            "print(sorted(m for m in sys.modules if m.startswith('imp.cli')))")
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                            check=True)
    assert result.stdout.strip() == "['imp.cli', 'imp.cli.narrative']"
