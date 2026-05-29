# tests/test_tui_copy.py
from __future__ import annotations

import base64

from imp.cli import narrative as nv
from imp.cli.tui import NarrativeApp, osc52_sequence, node_copy_text


def _ev(eid, etype, agent_id=None, **payload):
    return {"id": eid, "agent_id": agent_id, "type": etype, "payload": payload}


def test_osc52_sequence_base64_encodes_payload() -> None:
    seq = osc52_sequence("hello")
    assert seq.startswith("\x1b]52;c;")
    assert seq.endswith("\x07")
    b64 = seq[len("\x1b]52;c;"):-1]
    assert base64.b64decode(b64).decode() == "hello"


def test_node_copy_text_returns_full_agent_message() -> None:
    # Use a string without trailing whitespace so it survives AgentTextNode.text's
    # per-part .strip() — the intent is "not truncated for display", not
    # whitespace preservation.
    long = ("Full message body. " * 40).rstrip()
    tree = nv.build_tree([
        _ev("1", "phase_started", phase="p"),
        _ev("2", "step_started", step="s", model="m", provider="anthropic"),
        _ev("3", "llm_response", agent_id="step:p/s", provider="anthropic", model="m",
            stop_reason="end_turn", usage={}, raw={"content": [{"type": "text", "text": long}]}),
    ])
    step = tree.root.children[0].children[0]
    at = [c for c in step.children if isinstance(c, nv.AgentTextNode)][0]
    assert node_copy_text(at) == long           # full underlying text, untruncated


def test_node_copy_text_for_tool_burst_joins_calls() -> None:
    tree = nv.build_tree([
        _ev("1", "phase_started", phase="p"),
        _ev("2", "step_started", step="s", model="m", provider="anthropic"),
        _ev("3", "tool_call", agent_id="step:p/s", id="b1", name="bash",
            input={"command": "ls"}),
        _ev("4", "tool_result", agent_id="step:p/s", id="b1", content="file1\nfile2", error=False),
    ])
    step = tree.root.children[0].children[0]
    burst = [c for c in step.children if isinstance(c, nv.ToolBurstFold)][0]
    txt = node_copy_text(burst)
    assert "bash" in txt and "file1\nfile2" in txt


def test_copy_focused_node_writes_osc52_and_sets_status() -> None:
    captured = []
    tree = nv.build_tree([
        _ev("1", "phase_started", phase="p"),
        _ev("2", "step_started", step="s", model="m", provider="anthropic"),
        _ev("3", "llm_response", agent_id="step:p/s", provider="anthropic", model="m",
            stop_reason="end_turn", usage={}, raw={"content": [{"type": "text", "text": "copy me"}]}),
    ])
    app = NarrativeApp(tree, live=False, mouse=False, _osc_writer=captured.append)
    at_idx = [i for i, ln in enumerate(app._lines) if isinstance(ln.node, nv.AgentTextNode)][0]
    app.cursor_line = at_idx
    app.copy_focused_node()
    assert captured and "copy me" in __import__("base64").b64decode(
        captured[0][len("\x1b]52;c;"):-1]).decode()
    assert "copied" in app.status_note.lower()
