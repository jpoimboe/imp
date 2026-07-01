import typing
from pathlib import Path

import pytest

from imp.engine.errors import LoadError
from imp.engine.loader import _infer_kind, _output_types_for_step
from imp.engine.types import Kind, SourceLoc

_LOC = SourceLoc(file=Path("wf.yaml"), line=1)


def test_infer_kind_accepts_merge_and_exit_worktree() -> None:
    assert _infer_kind(
        {"kind": "merge_and_exit_worktree", "rules": []}, _LOC
    ) == "merge_and_exit_worktree"


def test_retired_kinds_are_gone() -> None:
    kind_args = typing.get_args(Kind)
    assert "wait_for_head" not in kind_args
    assert "dequeue_wake" not in kind_args
    assert "merge" not in kind_args
    assert "merge_and_exit_worktree" in kind_args


def test_engine_native_kind_rejects_file_or_command() -> None:
    with pytest.raises(LoadError, match="engine-native"):
        _infer_kind({"kind": "merge_and_exit_worktree", "command": "x"}, _LOC)


def test_merge_and_exit_worktree_has_typed_merge_output_env() -> None:
    assert _output_types_for_step("merge_and_exit_worktree", None) == {"merge": "string"}
    assert _output_types_for_step("branch", None) is None


def test_script_output_types_merge_declared_fields() -> None:
    from imp.engine.types import OutputField, OutputSchema
    schema = OutputSchema(
        fields=(OutputField(name="task_total", type="int"),), source=_LOC)
    assert _output_types_for_step("script", None, schema) == {
        "exit": "int", "stdout": "string", "stderr": "string",
        "task_total": "int",
    }


def test_script_output_types_excludes_object_typed_field() -> None:
    from imp.engine.types import OutputField, OutputSchema
    schema = OutputSchema(
        fields=(
            OutputField(name="metadata", type="object"),
            OutputField(name="task_total", type="int"),
        ),
        source=_LOC,
    )
    result = _output_types_for_step("script", None, schema)
    assert result == {
        "exit": "int", "stdout": "string", "stderr": "string",
        "task_total": "int",
    }
    assert "metadata" not in result


def test_script_output_types_default_without_schema() -> None:
    assert _output_types_for_step("script", None) == {
        "exit": "int", "stdout": "string", "stderr": "string",
    }
