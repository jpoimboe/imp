from __future__ import annotations

from pathlib import Path

from imp.agents.prompts import build_context, render_prompt
from imp.assets import bundled_workflow_dir
from imp.engine.loader import load_workflow
from imp.engine.state import JsonlEventLog


def _render(tmp_path: Path, *, non_interactive: bool) -> str:
    wf = load_workflow(bundled_workflow_dir("imp"))
    body = wf.agents["front-door"].prompt_template_source
    log = JsonlEventLog(tmp_path / "events.jsonl")
    return render_prompt(
        body,
        build_context(change_title="t", event_log=log, change_id="abc",
                      change_home=".imp/changes/abc",
                      non_interactive=non_interactive,
                      project_facts={}, guidance=""),
        workflow_dir=bundled_workflow_dir("imp"))


def test_headless_goes_live_without_build_vs_park_solicit(tmp_path: Path) -> None:
    out = _render(tmp_path, non_interactive=True)
    assert "create_and_enter_worktree" in out
    assert "imp resume <id>" not in out


def test_interactive_keeps_build_solicit(tmp_path: Path) -> None:
    out = _render(tmp_path, non_interactive=False)
    assert "build this out now" in out.lower()
    assert "create_and_enter_worktree" in out
    assert "imp resume <id>" in out
    assert "spec phase pauses" in out.lower()
