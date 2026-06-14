from __future__ import annotations

from imp.assets import default_workflow_dir
from imp.engine.loader import load_workflow


def _prompt(name: str) -> str:
    return load_workflow(default_workflow_dir()).agents[name].prompt_template_source


def test_spec_writer_forbids_tool_loading_narration() -> None:
    body = _prompt("spec-writer")
    assert "do not narrate loading or finding tools" in body
    # Verify the prohibition is in the right context — near the unseeded-open
    # and wait_for_user instruction, not buried elsewhere in the file.
    # Extract the paragraph/section around "do not narrate" and confirm it
    # also mentions the opening context.
    idx = body.index("do not narrate loading or finding tools")
    context = body[max(0, idx - 300) : idx + 300]
    # Confirms the prohibition is in the unseeded-open section (near wait_for_user).
    assert "wait_for_user" in context or "Open by asking" in context


def test_spec_reviewer_states_conclusion_once() -> None:
    body = _prompt("spec-reviewer")
    assert "State your verified conclusion once" in body
    # Verify it's in the ## Output section (near "findings").
    idx = body.index("State your verified conclusion once")
    context = body[max(0, idx - 300) : idx + 300]
    assert "findings" in context or "## Output" in context


def test_spec_writer_single_closing_summary() -> None:
    body = _prompt("spec-writer")
    assert "Emit a single closing summary" in body
    # Verify it's in the general guidance area (not in an unrelated section).
    idx = body.index("Emit a single closing summary")
    context = body[max(0, idx - 300) : idx + 300]
    assert "complete_step" in context or "web_search" in context or "summary" in context.lower()
