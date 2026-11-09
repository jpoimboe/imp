from imp.assets import bundled_workflow_dir
from imp.engine.loader import load_workflow
from imp.engine.types import Goto, Pause

_VALIDATE_MEMORY_COMMAND = (
    '"$IMP_PYTHON" -m imp.providers.validate_memory_cli "$IMP_PROJECT_ROOT"')


def _wf():
    return load_workflow(bundled_workflow_dir("memory"))


def test_memory_workflow_loads_with_name() -> None:
    assert _wf().name == "memory"


def test_memory_workflow_step_sequence() -> None:
    names = [s.name for s in _wf().flat_steps()]
    assert names == [
        "memory-editor", "validate-memory", "wait-for-head", "merge-to-base"]


def test_memory_workflow_labels() -> None:
    wf = _wf()
    assert {s.name: s.label for s in wf.flat_steps()} == {
        "memory-editor": "editing memory",
        "validate-memory": "checking memory files",
        "wait-for-head": "waiting in merge queue",
        "merge-to-base": "merging",
    }
    assert [p.label for p in wf.phases] == [None]


def test_memory_merge_step_is_engine_native() -> None:
    by = {s.name: s for s in _wf().flat_steps()}
    assert by["merge-to-base"].kind == "merge_and_exit_worktree"


def test_wait_for_head_is_branch_acquiring_merge_lane() -> None:
    by = {s.name: s for s in _wf().flat_steps()}
    assert by["wait-for-head"].kind == "branch"
    assert by["wait-for-head"].acquire == "merge-lane"


def test_validate_step_runs_memory_cli_under_engine_interpreter() -> None:
    by = {s.name: s for s in _wf().flat_steps()}
    assert by["validate-memory"].command == _VALIDATE_MEMORY_COMMAND


def test_validate_memory_commands_are_byte_identical_across_workflows() -> None:
    default = load_workflow(bundled_workflow_dir("default"))
    default_cmd = next(
        s for s in default.flat_steps() if s.name == "validate-memory").command
    memory_cmd = next(
        s for s in _wf().flat_steps() if s.name == "validate-memory").command
    assert default_cmd == memory_cmd == _VALIDATE_MEMORY_COMMAND


def test_memory_editor_error_rule_pauses() -> None:
    by = {s.name: s for s in _wf().flat_steps()}
    editor = by["memory-editor"]
    # The {if: {status: Error}, pause: true} rule has a non-None condition and Pause action.
    error_rule = editor.rules[0]
    assert error_rule.condition is not None
    assert isinstance(error_rule.action, Pause)


def test_merge_and_validate_resume_routing() -> None:
    by = {s.name: s for s in _wf().flat_steps()}

    # (b) merge-to-base: the else branch (non-MERGED verdict) pauses.
    merge = by["merge-to-base"]
    non_merged_rule = next(r for r in merge.rules if r.is_else)
    assert isinstance(non_merged_rule.action, Pause)

    # (c) validate-memory: the resume rule carries no explicit feedback string
    #     (feedback is auto-injected by the engine, not authored here).
    validate = by["validate-memory"]
    resume_rule = next(r for r in validate.rules if isinstance(r.action, Goto) and r.action.resume)
    assert resume_rule.action.feedback is None
