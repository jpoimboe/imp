from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from imp.assets import bundled_workflow_dir
from imp.engine.gates import evaluate_rules
from imp.engine.loader import load_workflow
from imp.engine.types import Continue, Goto

_BASE = {
    "fast_path": False, "plan_review_notes": "",
    "coverage_settled": False, "plan_index": 1, "plan_total": 0, "task_total": 0,
}


def _steps():
    wf = load_workflow(bundled_workflow_dir("default"))
    return {s.name: s for s in wf.flat_steps()}


def test_plan_finalized_step_is_a_show_output_script() -> None:
    step = _steps()["plan-finalized"]
    assert step.kind == "script"
    assert step.command.startswith("./scripts/plan-finalized.sh")
    assert step.show_output is True
    assert [f.name for f in step.output_schema.fields] == ["task_total"]
    assert step.output_schema.fields[0].type == "int"


def test_plan_finalized_sets_task_total_and_enters_implement() -> None:
    rules = _steps()["plan-finalized"].rules
    m = evaluate_rules(rules, vars=_BASE, output={"exit": 0, "stdout": "",
                                                  "stderr": "", "task_total": 6})
    assert isinstance(m.action, Goto) and m.action.target == "task-lead"
    assert m.var_mutations["task_total"] == 6


def test_fresh_plan_arrows_route_through_plan_finalized() -> None:
    steps = _steps()
    fast = evaluate_rules(steps["plan-verify-commit"].rules, vars={**_BASE, "fast_path": True},
                          output={"exit": 0, "stdout": "", "stderr": ""})
    assert fast.action.target == "plan-finalized"
    minors = evaluate_rules(steps["plan-reviewer"].rules, vars=_BASE,
                            output={"critical": 0, "major": 0, "minor": 2,
                                    "findings": "Minor: advisory"})
    assert minors.action.target == "plan-finalized"
    clean = evaluate_rules(
        steps["plan-reviewer"].rules, vars=_BASE,
        output={"critical": 0, "major": 0, "minor": 0,
                "findings": "No findings. Plan is complete and ready for implementation."})
    assert clean.action.target == "plan-finalized"


def test_task_loop_and_rework_bypass_plan_finalized() -> None:
    steps = _steps()
    loop = evaluate_rules(steps["task-lead"].rules, vars=_BASE,
                          output={"status": "Success", "done": True,
                                  "verify_tolerated": False, "summary": "x"})
    assert isinstance(loop.action, Continue)
    # quest-review's task_gap rework re-enters task-lead directly, not plan-finalized.
    gap = evaluate_rules(steps["quest-review"].rules, vars=_BASE,
                         output={"status": "Success", "verdict": "task_gap", "findings": "x"})
    assert gap.action.target == "task-lead"


def test_plan_finalized_agent_prompt_is_gone() -> None:
    assert not (bundled_workflow_dir("default") / "agents" / "plan-finalized.md").exists()


@pytest.mark.parametrize("workflow", ["default"])
def test_plan_finalized_script_counts_highest_plan_and_writes_output(tmp_path, workflow) -> None:
    script = Path(f"src/imp/assets/workflows/{workflow}/scripts/plan-finalized.sh").resolve()
    plans = tmp_path / "artifacts" / "plans"
    plans.mkdir(parents=True)
    (plans / "abc-plan-1.md").write_text("# P\n## Task 1: Alpha\nbody\n")
    (plans / "abc-plan-2.md").write_text(
        "# P2\n## Task 1: Beta\nbody\n## Task 2: Gamma\nmore\n")
    out_file = tmp_path / "out.json"
    env = {**os.environ,
           "IMP_PROJECT_ROOT": str(tmp_path),
           "IMP_QUEST_DIR": str(tmp_path),
           "IMP_QUEST_ID": "abc",
           "IMP_OUTPUT": str(out_file)}
    r = subprocess.run(["bash", str(script), "2", "3"],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    # highest-numbered plan is plan-2 (2 tasks), NOT plan-1 (1 task).
    assert r.stdout.splitlines()[0] == "✓ plan 2/3 · 2 tasks · imp show abc plans/abc-plan-2.md"
    assert "1. Beta" in r.stdout and "2. Gamma" in r.stdout
    assert json.loads(out_file.read_text()) == {"task_total": 2}


def test_plan_finalized_script_singular_for_one_task(tmp_path) -> None:
    script = Path("src/imp/assets/workflows/default/scripts/plan-finalized.sh").resolve()
    plans = tmp_path / "artifacts" / "plans"
    plans.mkdir(parents=True)
    (plans / "abc-plan-1.md").write_text("# P\n## Task 1: Alpha\nbody\n")
    out_file = tmp_path / "out.json"
    env = {**os.environ,
           "IMP_PROJECT_ROOT": str(tmp_path),
           "IMP_QUEST_DIR": str(tmp_path),
           "IMP_QUEST_ID": "abc",
           "IMP_OUTPUT": str(out_file)}
    r = subprocess.run(["bash", str(script), "1", "1"],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines()[0] == "✓ plan 1/1 · 1 task · imp show abc plans/abc-plan-1.md"
    assert json.loads(out_file.read_text()) == {"task_total": 1}


def test_plan_finalized_header_names_the_plan_file(tmp_path) -> None:
    script = Path("src/imp/assets/workflows/default/scripts/plan-finalized.sh").resolve()
    plans = tmp_path / "artifacts" / "plans"
    plans.mkdir(parents=True)
    (plans / "zuza70-plan-1.md").write_text(
        "# P\n## Task 1: Alpha\nbody\n## Task 2: Beta\nmore\n")
    out_file = tmp_path / "out.json"
    env = {**os.environ,
           "IMP_PROJECT_ROOT": str(tmp_path),
           "IMP_QUEST_DIR": str(tmp_path),
           "IMP_QUEST_ID": "zuza70",
           "IMP_OUTPUT": str(out_file)}
    r = subprocess.run(["bash", str(script), "1", "1"],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines() == [
        "✓ plan 1/1 · 2 tasks · imp show zuza70 plans/zuza70-plan-1.md",
        "   1. Alpha",
        "   2. Beta",
    ]


def test_plan_finalized_header_keeps_the_real_file_name(tmp_path) -> None:
    script = Path("src/imp/assets/workflows/default/scripts/plan-finalized.sh").resolve()
    plans = tmp_path / "artifacts" / "plans"
    plans.mkdir(parents=True)
    (plans / "zuza70-plan-02.md").write_text("# P\n## Task 1: Alpha\nbody\n")
    out_file = tmp_path / "out.json"
    env = {**os.environ,
           "IMP_PROJECT_ROOT": str(tmp_path),
           "IMP_QUEST_DIR": str(tmp_path),
           "IMP_QUEST_ID": "zuza70",
           "IMP_OUTPUT": str(out_file)}
    r = subprocess.run(["bash", str(script), "2", "2"],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines()[0] == (
        "✓ plan 2/2 · 1 task · imp show zuza70 plans/zuza70-plan-02.md")
