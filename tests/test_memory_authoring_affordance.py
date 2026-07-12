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
