from __future__ import annotations

from imp.cli import narrative as nv
from imp.cli.render_text import render_tree
from tests._event_fixtures import ev

NOTICE = "plan 2/4 · 2 tasks\n   1. add widget\n   2. wire widget"


def _events() -> list[dict]:
    return [
        ev("1", "change_created", change_id=1, title="x"),
        ev("2", "step_started", step="plan-finalized", kind="agent",
           phase="plan", model="m", provider="anthropic", label="finalizing plan"),
        ev("3", "notice", agent_id="step:plan/plan-finalized", text=NOTICE),
    ]


def _lead(s: str) -> int:
    return len(s) - len(s.lstrip(" "))


def test_notice_appends_verbatim_literal_text_node() -> None:
    b = nv.NarrativeBuilder()
    for e in _events():
        b.add(e)
    root = b.tree.root
    literals = [c for ph in root.children if isinstance(ph, nv.PhaseNode)
                for st in ph.children if isinstance(st, nv.StepNode)
                for c in st.children if isinstance(c, nv.LiteralTextNode)]
    assert any(n.text == NOTICE and n.verbatim for n in literals)


def test_notice_render_preserves_indentation() -> None:
    tree = nv.build_tree(_events())
    lines = render_tree(tree, full=True, color=False, width=80)
    header = next(ln for ln in lines if "plan 2/4 · 2 tasks" in ln)
    task = next(ln for ln in lines if "1. add widget" in ln)
    # Verbatim: the task line keeps its 3 extra leading spaces over the header.
    # The shared gutter is identical for both lines, so the +3 is the content
    # indent that _wrap(drop_whitespace) would have stripped to 0.
    assert _lead(task) == _lead(header) + 3
