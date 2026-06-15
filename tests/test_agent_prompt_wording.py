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


def test_spec_writer_writes_to_artifacts_and_uses_commit_script() -> None:
    body = _agent_prompt("spec-writer")
    assert ".imp/change/artifacts/spec/" in body
    assert "imp artifact-commit spec" in body
    # The old code-branch path and git-commit machinery are gone.
    assert "imp/specs/" not in body
    assert "git commit --amend" not in body
