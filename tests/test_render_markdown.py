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


def test_agent_prose_escaped_and_passed_through() -> None:
    out = _md([
        _ev("2", "step_started", step="s", kind="agent", phase="p",
            model="m", provider="anthropic"),
        _ev("3", "llm_response", agent_id="step:p/s", provider="anthropic", model="m",
            stop_reason="end_turn", usage={},
            raw={"content": [{"type": "text",
                              "text": "hello **world** and <b>x</b>"}]}),
    ])
    assert "hello **world**" in out       # markdown formatting survives (not escaped)
    assert "&lt;b&gt;x&lt;/b&gt;" in out   # inline HTML is escaped
    assert "<b>x</b>" not in out


def test_hostile_agent_prose_is_escaped() -> None:
    out = _md([
        _ev("2", "step_started", step="s", kind="agent", phase="p",
            model="m", provider="anthropic"),
        _ev("3", "llm_response", agent_id="step:p/s", provider="anthropic", model="m",
            stop_reason="end_turn", usage={},
            raw={"content": [{"type": "text",
                              "text": "<script>alert(1)</script>\x00tail"}]}),
    ])
    assert "&lt;script&gt;" in out
    assert "<script>" not in out
    assert "\x00" not in out
    assert "^@" in out


def test_genuine_user_turn_is_blockquote_synthetic_dropped() -> None:
    out = _md([
        _ev("1", "step_started", step="s", kind="agent", phase="p", model="m"),
        _ev("2", "user_message", agent_id="step:p/s",
            content=[{"type": "text", "text": "begin"}]),          # synthetic kickoff
        _ev("3", "awaiting_user_input", agent_id="step:p/s"),
        _ev("4", "user_message", agent_id="step:p/s",
            content=[{"type": "text", "text": "do the real thing"}]),
    ])
    assert "> do the real thing" in out    # genuine human turn → blockquote
    assert "begin" not in out              # synthetic prompt dropped


def test_user_turn_escaped_and_multiline_blockquote() -> None:
    out = _md([
        _ev("1", "step_started", step="s", kind="agent", phase="p", model="m"),
        _ev("2", "awaiting_user_input", agent_id="step:p/s"),
        _ev("3", "user_message", agent_id="step:p/s",
            content=[{"type": "text", "text": "<b>bad</b>\nsecond line"}]),
    ])
    assert "> &lt;b&gt;bad&lt;/b&gt;" in out   # untrusted HTML escaped inside blockquote
    assert "<b>bad</b>" not in out
    assert "> second line" in out             # multi-line prefixed per line


def test_literal_text_node_is_escaped() -> None:
    root = nv.WorkflowNode(title="t")
    root.children = [nv.LiteralTextNode(text="<script>x</script>")]
    tree = nv.NarrativeTree(root=root, tokens=nv.TokenTotals())
    out = render_markdown(tree, full=False)
    assert "&lt;script&gt;x&lt;/script&gt;" in out
    assert "<script>" not in out
