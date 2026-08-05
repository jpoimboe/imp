from __future__ import annotations

from pathlib import Path

import pytest

from imp.assets import bundled_workflow_dir
from imp.engine.gates import evaluate_rules
from imp.engine.loader import load_workflow
from imp.engine.types import Continue, Goto

AGENTS_DIR = Path("src/imp/assets/workflows/default/agents")


def _wf():
    return load_workflow(bundled_workflow_dir("default"))


def _step(name: str):
    return {s.name: s for s in _wf().flat_steps()}[name]


def _script_quest(tmp_path: Path, spec_body: str | None):
    """A minimal quest for driving the real gate step through _route_script."""
    import types

    home = tmp_path / "quest"
    (home / "artifacts" / "spec").mkdir(parents=True, exist_ok=True)
    if spec_body is not None:
        (home / "artifacts" / "spec" / "test-id.md").write_text(spec_body)
    (tmp_path / "tmp").mkdir(parents=True, exist_ok=True)
    return types.SimpleNamespace(
        workflow_dir=bundled_workflow_dir("default"), project_root=tmp_path,
        id="test-id", handle="test", title="Test", quest_dir=home,
        agent_quest_home=".imp/quests/test-quest", tmp_dir=tmp_path / "tmp",
        control_root=None)


def test_spec_gate_declares_optional_int_plan_total() -> None:
    schema = _step("spec-verify-commit").output_schema
    assert schema is not None
    fields = {f.name: f for f in schema.fields}
    assert "plan_total" in fields
    assert fields["plan_total"].type == "int"
    assert fields["plan_total"].required is False


def test_spec_gate_seeds_plan_total_on_exit_zero() -> None:
    m = evaluate_rules(_step("spec-verify-commit").rules, vars={},
                       output={"exit": 0, "stdout": "", "stderr": "",
                               "plan_total": 6})
    assert m.var_mutations == {"plan_total": 6}
    assert isinstance(m.action, Continue)


def test_spec_gate_failure_resumes_spec_finalizer_without_seeding() -> None:
    m = evaluate_rules(_step("spec-verify-commit").rules, vars={},
                       output={"exit": 1, "stdout": "", "stderr": "no outline"})
    assert "plan_total" not in m.var_mutations
    assert isinstance(m.action, Goto)
    assert m.action.target == "spec-finalizer"
    assert m.action.resume is True


def test_plan_writer_schema_does_not_declare_plan_total() -> None:
    assert "plan_total" not in _wf().agents["plan-writer"].output_schema.field_names()


def test_plan_writer_never_mutates_plan_total() -> None:
    rules = _step("plan-writer").rules
    for index in (1, 2, 7):
        m = evaluate_rules(rules, vars={"plan_index": index},
                           output={"plan_total": 99})
        assert "plan_total" not in m.var_mutations, index
        assert isinstance(m.action, Continue)


@pytest.mark.parametrize("spec_body", [None, "# spec\n\nbody with no outline\n"],
                         ids=["missing-spec", "no-outline"])
def test_failing_spec_gate_routes_instead_of_pausing(
        tmp_path: Path, spec_body: str | None) -> None:
    """The whole point of the script's seeded {}: a declared output makes the engine
    read $IMP_OUTPUT on EVERY exit, so an unwritten one would pause before the rules
    ever run. Both failure branches must still reach the else rule."""
    import types

    import anyio

    from imp.engine.executor import _route_script
    from imp.engine.state import InMemoryEventLog

    step = _step("spec-verify-commit")
    assert step.output_schema is not None      # the declared output is what creates the risk

    async def main() -> None:
        log = InMemoryEventLog()
        action, feedback, _ = await _route_script(
            step, _script_quest(tmp_path, spec_body),
            types.SimpleNamespace(vars={}, route_label=None), log)
        assert isinstance(action, Goto)
        assert action.target == "spec-finalizer" and action.resume is True
        assert "script_blocked" not in [e["type"] for e in log.read()]
        assert feedback is not None            # the script's stderr reaches the agent

    anyio.run(main)


def test_plan_writer_prompt_carries_no_plan_total_contract() -> None:
    text = (AGENTS_DIR / "plan-writer.md").read_text()
    low = text.lower()
    # The seeding contract moved to the spec gate: no section, no emitted argument.
    assert "first-run plan_total estimate" not in low
    assert "plan_total=" not in text
    assert "default `plan_total = 1`" not in text
    # The display references to the counter survive.
    assert "{{ vars.plan_total }}" in text


def test_spec_finalizer_and_gap_check_name_the_spec_gate_as_seeder() -> None:
    spec_finalizer = (AGENTS_DIR / "spec-finalizer.md").read_text()
    gap_check = (AGENTS_DIR / "gap-check.md").read_text()
    assert "the spec gate counts the outline" in spec_finalizer
    assert "the first `plan-writer` reads it and echoes the count" not in spec_finalizer
    # The outline's shape is stated where it is authored: top-level numbered items.
    assert "the spec gate counts them to seed the plan counter" in spec_finalizer
    assert "counted by the spec gate" in gap_check
    assert "plan-1 echo" not in gap_check
