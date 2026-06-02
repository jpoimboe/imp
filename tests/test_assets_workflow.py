from __future__ import annotations

import shutil
from pathlib import Path

from imp.engine.loader import load_workflow


def test_shipped_default_workflow_loads(tmp_path: Path) -> None:
    asset = Path("src/imp/assets/workflows/default").resolve()
    shutil.copytree(asset, tmp_path / "wf")
    wf = load_workflow(tmp_path / "wf")
    assert wf.name
    assert wf.phases
    for agent in wf.agents.values():
        assert "complete_step" in agent.tools, f"{agent.name} missing complete_step"


def test_spec_writer_does_not_declare_ask_user(tmp_path: Path) -> None:
    asset = Path("src/imp/assets/workflows/default").resolve()
    shutil.copytree(asset, tmp_path / "wf")
    wf = load_workflow(tmp_path / "wf")
    spec_writer = wf.agents["spec-writer"]
    assert "ask_user" not in spec_writer.tools
    assert "complete_step" in spec_writer.tools


def test_bundled_workflow_loads_and_producers_resume() -> None:
    from imp.assets import default_workflow_dir
    from imp.engine.types import Goto
    wf_dir = default_workflow_dir()
    wf = load_workflow(wf_dir)
    by = {s.name: s for s in wf.flat_steps()}
    # producer back-edges resume their target's conversation
    assert isinstance(by["spec-verify-commit"].rules[-1].action, Goto)
    assert by["spec-verify-commit"].rules[-1].action.resume is True   # -> spec-writer
    assert by["plan-verify-commit"].rules[-1].action.resume is True   # -> plan-writer
    assert by["run-tests"].rules[-1].action.resume is True            # -> task-lead
    assert by["spec-reviewer"].rules[0].action.resume is True         # rework -> spec-writer
    # phase jumps clear (plain goto)
    assert by["code-reviewer"].rules[1].action.resume is False    # rework -> plan-writer
    assert by["spec-verify-commit"].kind == "script"
    assert wf.phases[-1].name == "merge" and wf.phases[-1].steps == ()


def test_default_workflow_has_code_review_constraint():
    from imp.assets import default_workflow_dir
    from imp.engine.loader import load_workflow
    wf = load_workflow(default_workflow_dir())
    # code_review must differ from fast (the implementer subagent tier) for
    # cross-provider review; in the feature workflow the coder runs at the
    # `fast` tier, so the reviewer is constrained against `fast` not `code`.
    assert wf.model_rules["code_review"].differ_provider_from == "fast"


def test_default_code_reviewer_step_declares_model_role():
    # In the feature workflow model_role is assigned at the step level in
    # workflow.yaml, not in the agent frontmatter.  Verify the step-level
    # declaration is present and correct so code_review diversity wiring works.
    from imp.assets import default_workflow_dir
    from imp.engine.loader import load_workflow
    wf = load_workflow(default_workflow_dir())
    by = {s.name: s for s in wf.flat_steps()}
    assert by["code-reviewer"].model_role == "code_review"


def test_default_workflow_declares_tier_menu() -> None:
    from imp.assets import default_workflow_dir
    from imp.engine.loader import load_workflow
    wf = load_workflow(default_workflow_dir())
    assert wf.model_tiers == ("fast", "balanced", "powerful")
