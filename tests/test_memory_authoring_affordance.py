from __future__ import annotations

from imp.agents.prompts import build_context, render_prompt
from imp.assets import bundled_workflow_dir
from imp.engine.loader import load_workflow
from imp.engine.state import InMemoryEventLog

# Derive the partial path from the bundled workflow dir (the same
# FileSystemLoader root the include resolves against), so the test does not
# depend on pytest's cwd.
PARTIAL_PATH = bundled_workflow_dir("imp") / "partials" / "memory-authoring.md"


def test_memory_authoring_partial_has_pinned_prose() -> None:
    text = PARTIAL_PATH.read_text()
    assert text.startswith("## Recording standing preferences to memory")
    # Distinctive pinned phrases spanning each section of the approved text.
    assert 'language like "always,"' in text
    assert "Work out the target file (narrowest scope that fits):" in text
    assert "be reluctant with either `all.md`" in text
    assert "Never invent a step name." in text
    assert "only `commit_memory` takes the lock" in text
    assert "never `.imp/`" in text


def _render_agent(name: str, vars: dict) -> str:
    wf = load_workflow(bundled_workflow_dir("imp"))
    body = wf.agents[name].prompt_template_source
    return render_prompt(
        body,
        build_context(
            quest_title="t",
            event_log=InMemoryEventLog(),
            quest_id="x",
            quest_home=".imp/quests/x",
            vars=vars,
        ),
        workflow_dir=bundled_workflow_dir("imp"),
    )


def test_commit_memory_is_fully_retired() -> None:
    from imp.agents.tools import get_registry
    from imp.engine.loader import ALLOWABLE_TOOLS

    assert "commit_memory" not in get_registry()
    assert "commit_memory" not in ALLOWABLE_TOOLS
    wf = load_workflow(bundled_workflow_dir("imp"))
    assert "commit_memory" not in wf.agents["threshold"].tools
    assert "commit_memory" not in wf.agents["finalize"].tools


def test_threshold_prompt_includes_memory_authoring() -> None:
    out = _render_agent("threshold", {})
    assert "## Recording standing preferences to memory" in out


def test_finalize_prompt_includes_memory_authoring() -> None:
    # finalize's memory-authoring include is top-level (outside the autonomous
    # and auto_merge gates), so it renders on every path; pass a concrete vars
    # dict so the rest of the finalize template renders cleanly.
    out = _render_agent("finalize", {"auto_merge": False})
    assert "## Recording standing preferences to memory" in out


def test_threshold_access_permits_memory_writes() -> None:
    out = _render_agent("threshold", {})
    # The Access section now carves out imp/memory writes via commit_memory,
    # still forbidding raw git and .imp/.
    assert "one narrow exception" in out
    assert "record through `commit_memory`" in out
