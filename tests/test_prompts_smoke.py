from __future__ import annotations

from pathlib import Path

from imp.agents.prompts import build_context, render_prompt
from imp.engine.loader import parse_agent_file
from imp.engine.state import JsonlEventLog
from tests.conftest import write_agent_md


def test_render_uses_change_namespace(tmp_path: Path) -> None:
    p = write_agent_md(
        tmp_path / "a.md",
        frontmatter=(
            "name: a\ntools: [complete_step]\noutput:\n"
            "  status: {type: enum, values: [Success, Error]}"
        ),
        body="Change {{ change.id }}: {{ change.title }}",
    )
    agent = parse_agent_file(p)
    log = JsonlEventLog(tmp_path / "events.jsonl")
    ctx = build_context(quest_title="foo", event_log=log, quest_id="my-id")
    rendered = render_prompt(agent.prompt_template_source, ctx)
    assert "Change my-id: foo" in rendered
