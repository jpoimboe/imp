from __future__ import annotations

from pathlib import Path

from imp.agents.prompts import build_context, render_prompt
from imp.assets import bundled_workflow_dir
from imp.engine.loader import load_workflow
from imp.engine.state import JsonlEventLog


def _section(rendered: str) -> str:
    start = rendered.find("## Build now or save for later")
    end = rendered.find("## Writing the Spec", start)
    assert start != -1 and end != -1
    return rendered[start:end]


def _render(tmp_path: Path, *, non_interactive: bool) -> str:
    wf = load_workflow(bundled_workflow_dir("imp"))
    body = wf.agents["spec-writer"].prompt_template_source
    log = JsonlEventLog(tmp_path / "events.jsonl")
    return render_prompt(
        body,
        build_context(change_title="t", event_log=log,
                      non_interactive=non_interactive),
    )


def test_headless_goes_live_without_build_vs_park_solicit(tmp_path: Path) -> None:
    block = _section(_render(tmp_path, non_interactive=True))
    assert "create_and_enter_worktree" in block
    assert 'complete_step(phase="written"' in block
    # The headless branch drops the build-or-save solicit and the save option.
    assert "wait_for_user" not in block
    assert "Save as a draft" not in block
    assert "go live" not in block.lower()


def test_interactive_keeps_build_solicit(tmp_path: Path) -> None:
    block = _section(_render(tmp_path, non_interactive=False))
    assert "wait_for_user" in block
    assert "create_and_enter_worktree" in block

    assert "build this out now" in block.lower()
    assert "1. **Build it now**" not in block
    assert "2. **Save as a draft**" not in block

    assert "imp resume <id>" in block
