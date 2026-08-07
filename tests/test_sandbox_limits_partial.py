"""The sandbox-limits partial states imp's write bounds ahead of the escape hatches,
and the old partial name survives nowhere under src/ or tests/."""

from __future__ import annotations

from pathlib import Path

import pytest

from imp.agents.prompts import build_context, render_prompt
from imp.assets import bundled_workflow_dir
from imp.engine.loader import load_workflow
from imp.engine.state import InMemoryEventLog

PARTIAL = Path("src/imp/assets/partials/sandbox-limits.md")
# Split so the stale-name scan below never matches its own source file.
_OLD_NAME = "sandbox-" + "debugging"


def _flat(text: str) -> str:
    return " ".join(text.split())


def test_partial_states_the_write_bounds_ahead_of_the_hatches() -> None:
    flat = _flat(PARTIAL.read_text())
    assert flat.startswith("## What you cannot write")
    assert (
        "binds all of `.git/refs/heads`, `.git/refs/stash`, and the main "
        "`.git/index` read-only" in flat)
    assert "re-exposing only `refs/heads/imp/`" in flat
    # The doctrine sentence: base refs are the engine's and the user's alone.
    assert "Base branches move only by imp's engine or by the user — never by you." in flat
    assert "give the user the exact command to run in their own shell" in flat
    # git's misleading error is called out, so an agent reads a denial correctly.
    assert "blames a write error rather than a permission" in flat
    # The escape-hatch section appears below the write-bounds section.
    assert flat.index("## What you cannot write") < flat.index("## When the sandbox blocks you")


def test_no_source_or_test_file_references_the_old_partial_name() -> None:
    assert not (PARTIAL.parent / f"{_OLD_NAME}.md").exists()
    hits = [
        str(p)
        for root in (Path("src"), Path("tests"))
        for p in root.rglob("*")
        if p.is_file() and p.suffix in {".md", ".py", ".yaml", ".yml", ".sh"}
        and _OLD_NAME in p.read_text(errors="ignore")
    ]
    assert hits == []


# Every interactive agent that runs git writes, across all three workflows.
_SITES = (
    ("default", "threshold", {}),
    ("default", "finalize", {"auto_merge": False}),
    ("minimal", "front-door", {}),
    ("minimal", "finalize", {}),
    ("patent", "invention-interviewer", {}),
    ("patent", "finalize-gate", {}),
)


@pytest.mark.parametrize(("workflow", "agent", "extra_vars"), _SITES)
def test_interactive_git_writing_agents_render_the_write_bounds(
        workflow, agent, extra_vars) -> None:
    wf_dir = bundled_workflow_dir(workflow)
    body = load_workflow(wf_dir).agents[agent].prompt_template_source
    out = render_prompt(
        body,
        build_context(quest_title="t", event_log=InMemoryEventLog(), quest_id="abc",
                      quest_home=".imp/quests/abc", vars=extra_vars),
        workflow_dir=wf_dir)
    # A sentence only the write-bounds section carries: the include resolved here.
    assert "Base branches move only by imp's engine" in out
    assert "{% include" not in out
    # Fragments only the reframed hatch section carries — the include pulls it in too.
    assert "or a read of a masked path" in out
    assert "re-enables itself when the step ends" in out
