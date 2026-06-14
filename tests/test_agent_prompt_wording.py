from __future__ import annotations

from imp.assets import default_workflow_dir
from imp.engine.loader import load_workflow


def _agent_prompt(name: str) -> str:
    wf = load_workflow(default_workflow_dir())
    return wf.agents[name].prompt_template_source


def test_finalize_prompt_has_plain_language_guidance() -> None:
    body = _agent_prompt("finalize")
    assert "never surface internal gate jargon" in body
    assert "no base changes to integrate" in body


def test_finalize_prompt_avoids_full_width_rules() -> None:
    body = _agent_prompt("finalize")
    assert "do **not** draw full-width horizontal rules" in body
