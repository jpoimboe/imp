from __future__ import annotations

from pathlib import Path

from imp.agents.prompts import build_context, render_prompt
from imp.assets import bundled_workflow_dir
from imp.engine.loader import load_workflow
from imp.engine.state import JsonlEventLog


def _render(tmp_path: Path, *, autonomous: bool = False) -> str:
    wf = load_workflow(bundled_workflow_dir("imp"))
    body = wf.agents["threshold"].prompt_template_source
    log = JsonlEventLog(tmp_path / "events.jsonl")
    return render_prompt(
        body,
        build_context(quest_title="t", event_log=log, quest_id="abc",
                      quest_home=".imp/quests/abc",
                      autonomous=autonomous,
                      project_facts={}),
        workflow_dir=bundled_workflow_dir("imp"))


def test_attended_keeps_build_solicit(tmp_path: Path) -> None:
    out = _render(tmp_path)
    assert "build this out now" in out.lower()
    assert "create_and_enter_worktree" in out
    assert "imp resume <id>" in out
    assert "spec phase pauses" in out.lower()


def test_autonomous_takes_build_default_without_solicit(tmp_path: Path) -> None:
    out = _render(tmp_path, autonomous=True)
    assert "create_and_enter_worktree" in out
    assert "Want me to build this out now?" not in out
    assert "take the default and proceed without yielding" in out.lower()
