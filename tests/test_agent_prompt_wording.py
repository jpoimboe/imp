from __future__ import annotations

from imp.assets import bundled_workflow_dir
from imp.engine.loader import load_workflow


def _agent_prompt(name: str) -> str:
    wf = load_workflow(bundled_workflow_dir("imp"))
    return wf.agents[name].prompt_template_source


def test_finalize_prompt_has_plain_language_guidance() -> None:
    body = _agent_prompt("finalize")
    assert "never surface internal gate jargon" in body
    assert "own diff" in body


def test_finalize_prompt_avoids_full_width_rules() -> None:
    body = _agent_prompt("finalize")
    assert "do **not** draw full-width horizontal rules" in body


def test_spec_writer_writes_to_artifacts_and_uses_commit_script() -> None:
    body = _agent_prompt("spec-writer")
    assert "{{ change.home }}/artifacts/spec/" in body
    assert "imp artifact-commit spec" in body
    # The old code-branch path and git-commit machinery are gone.
    assert "imp/specs/" not in body
    assert "git commit --amend" not in body


def test_plan_writer_writes_to_artifacts_and_uses_commit_script() -> None:
    body = _agent_prompt("plan-writer")
    assert "{{ change.home }}/artifacts/plans/" in body
    assert "imp artifact-commit plan" in body
    # No plan fixup / amend machinery survives.
    assert "--fixup" not in body
    assert "git commit --amend" not in body
    assert "imp/plans/" not in body


def test_plan_writer_decides_and_records_scope_contradictions() -> None:
    body = _agent_prompt("plan-writer")
    assert "correction-with-a-default" in body
    assert "marginal cost" in body
    assert "ships a broken feature" in body
    assert "ride to the" in body
    # Stale "authoritative for that point" phrase is reconciled away.
    assert "authoritative for that point" not in body


def test_both_prompts_route_recorded_scope_deviations_to_the_gate() -> None:
    writer = _agent_prompt("plan-writer")
    reviewer = _agent_prompt("plan-reviewer")
    # No contradiction between the two: both route a recorded scope deviation to
    # the merge gate rather than re-litigating it in the loop.
    assert "ride to the" in writer
    assert "ride to the" in reviewer


def test_no_agent_prompt_references_code_branch_artifact_paths() -> None:
    wf = load_workflow(bundled_workflow_dir("imp"))
    for name, agent in wf.agents.items():
        body = agent.prompt_template_source
        assert "imp/specs/" not in body, f"{name} still references imp/specs/"
        assert "imp/plans/" not in body, f"{name} still references imp/plans/"
