from __future__ import annotations

from imp.cli import narrative as nv
from imp.cli.render_text import (
    deferred_leaf,
    frame_suppressed,
    render_tree,
    step_hidden,
    step_no_body,
)
from tests._event_fixtures import ev


def _body(events) -> list[str]:
    return list(render_tree(nv.build_tree(events), full=False, color=False, width=80))


def test_show_label_false_script_silent_on_linear_continue() -> None:
    body = _body([
        ev("1", "change_created", change_id=1, title="x"),
        ev("2", "step_started", step="cleanup", kind="script", phase="implement",
           show_label=False),
        ev("3", "step_completed", step="cleanup", kind="script",
           action={"verb": "continue"}, var_mutations={}),
    ])
    assert not any("cleanup" in ln for ln in body)


def test_show_label_false_script_reveals_frame_and_error_on_pause() -> None:
    body = _body([
        ev("1", "change_created", change_id=1, title="x"),
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
        ev("1", "change_created", change_id=1, title="x"),
        ev("2", "step_started", step="scoping", kind="agent", phase="front-door",
           model="m", provider="anthropic", label="chatting", show_label=False),
        ev("3", "llm_response", agent_id="step:front-door/scoping",
           provider="anthropic", model="m", stop_reason="end_turn", usage={},
           raw={"content": [{"type": "text", "text": "how can I help"}]}),
        ev("4", "step_completed", step="scoping", kind="agent",
           action={"verb": "continue"}, var_mutations={}),
    ])
    out = "\n".join(body)
    assert "how can I help" in out    # conversation body survives
    assert "chatting ·" not in out    # step header/label suppressed


# --- predicate unit tests ---

def test_default_step_not_hidden_not_deferred() -> None:
    node = nv.StepNode(name="s", kind="script", show_label=True, quiet=False)
    assert step_hidden(node) is False
    assert deferred_leaf(node) is False


def test_legacy_quiet_flag_counts_as_deferred_leaf() -> None:
    # quiet=True with show_label=True and no show_output: same semantics as old `node.quiet`
    node = nv.StepNode(name="s", kind="script", show_label=True, quiet=True, show_output=False)
    assert deferred_leaf(node) is True


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
