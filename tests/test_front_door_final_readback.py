from __future__ import annotations

from pathlib import Path

from imp.agents.prompts import build_context, render_prompt
from imp.assets import bundled_workflow_dir
from imp.engine.loader import load_workflow
from imp.engine.state import JsonlEventLog


def _render(tmp_path: Path) -> str:
    wf = load_workflow(bundled_workflow_dir("imp"))
    body = wf.agents["front-door"].prompt_template_source
    log = JsonlEventLog(tmp_path / "events.jsonl")
    return render_prompt(
        body,
        build_context(change_title="t", event_log=log, change_id="abc",
                      change_home=".imp/changes/abc", non_interactive=False,
                      project_facts={}, guidance=""),
        workflow_dir=bundled_workflow_dir("imp"))


def test_final_readback_present_and_consolidated(tmp_path: Path) -> None:
    out = _render(tmp_path)
    start = out.find("**Final readback before committing.**")
    assert start != -1
    block = out[start:start + 1200].lower()
    assert "consolidated" in block
    for token in ("goal", "scope", "decision", "acceptance"):
        assert token in block
    assert "wait_for_user" in block
    assert "not another round" in block
    assert "trivial" in block
