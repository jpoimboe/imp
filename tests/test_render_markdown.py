# tests/test_render_markdown.py
from __future__ import annotations

from imp.cli import narrative as nv
from imp.cli.render_markdown import _md_escape, render_markdown


def _ev(eid, etype, agent_id=None, **payload):
    return {"id": eid, "agent_id": agent_id, "type": etype, "payload": payload}


def _md(events, *, full=False):
    tree = nv.build_tree(events)
    return render_markdown(tree, full=full)


def _in_code_fence(md: str, needle: str) -> bool:
    """True when `needle` appears on a line that sits inside a ``` fence."""
    inside = False
    for line in md.split("\n"):
        if line.lstrip().startswith("```"):
            inside = not inside
            continue
        if inside and needle in line:
            return True
    return False


def test_md_escape_neutralizes_html_and_control_bytes() -> None:
    out = _md_escape("<script>&\x00")
    assert "&lt;script&gt;" in out
    assert "&amp;" in out
    assert "<script>" not in out
    assert "\x00" not in out
    assert "^@" in out            # C0/DEL caret-escaped via render_text._caret_escape


def test_heading_spine_workflow_phase_step() -> None:
    out = _md([
        _ev("1", "change_created", title="add auth"),
        _ev("2", "step_started", step="spec", kind="agent", phase="design",
            model="m", provider="anthropic"),
        _ev("3", "step_completed", step="spec", kind="agent",
            action={"verb": "continue"}, var_mutations={}),
    ])
    assert "# add auth" in out          # WorkflowNode → level-1 heading
    assert "## design" in out           # PhaseNode → level-2 heading
    assert "### spec" in out            # StepNode → level-3 heading


def test_heading_content_is_escaped_end_to_end() -> None:
    out = _md([
        _ev("1", "change_created", title="<evil>&"),
        _ev("2", "step_started", step="spec", kind="agent", phase="design",
            model="m", provider="anthropic"),
        _ev("3", "step_completed", step="spec", kind="agent",
            action={"verb": "continue"}, var_mutations={}),
    ])
    assert "# &lt;evil&gt;&amp;" in out
    assert "# <evil>" not in out
