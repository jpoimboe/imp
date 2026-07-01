from __future__ import annotations

from imp.cli import narrative as nv
from tests._event_fixtures import ev


def _step(tree: nv.NarrativeTree, name: str) -> nv.StepNode:
    for ph in tree.root.children:
        if isinstance(ph, nv.PhaseNode):
            for st in ph.children:
                if isinstance(st, nv.StepNode) and st.name == name:
                    return st
    raise AssertionError(f"no step {name!r}")


def test_show_label_and_show_output_land_on_step_node() -> None:
    tree = nv.build_tree([
        ev("1", "change_created", change_id=1, title="x"),
        ev("2", "step_started", step="announce", kind="script", phase="plan",
           show_label=False, show_output=True),
        ev("3", "step_completed", step="announce", kind="script",
           action={"verb": "continue"}, var_mutations={}),
    ])
    st = _step(tree, "announce")
    assert st.show_label is False
    assert st.show_output is True


def test_resume_reentry_restarted_updates_show_label_and_show_output() -> None:
    tree = nv.build_tree([
        ev("1", "change_created", change_id=1, title="x"),
        ev("2", "step_started", step="coder", kind="agent", phase="build",
           model="m", provider="anthropic"),
        ev("3", "workflow_paused", reason="ctrl-c"),
        ev("4", "workflow_resumed"),
        ev("5", "step_started", step="coder", kind="agent", phase="build",
           model="m", provider="anthropic",
           resumed=True, restarted=True, show_label=False, show_output=True),
    ])
    st = _step(tree, "coder")
    assert st.show_label is False
    assert st.show_output is True


def test_show_label_defaults_true_and_show_output_defaults_false() -> None:
    tree = nv.build_tree([
        ev("1", "change_created", change_id=1, title="x"),
        ev("2", "step_started", step="spec-writer", kind="agent", phase="spec",
           model="m", provider="anthropic"),
        ev("3", "step_completed", step="spec-writer", kind="agent",
           action={"verb": "continue"}, var_mutations={}),
    ])
    st = _step(tree, "spec-writer")
    assert st.show_label is True
    assert st.show_output is False
