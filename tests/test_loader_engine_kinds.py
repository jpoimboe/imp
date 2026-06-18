import typing
from pathlib import Path

import pytest

from imp.engine.errors import LoadError
from imp.engine.loader import _infer_kind, _output_types_for_step
from imp.engine.types import Kind, SourceLoc

_LOC = SourceLoc(file=Path("wf.yaml"), line=1)


def test_infer_kind_accepts_wait_for_head_and_dequeue_wake() -> None:
    assert _infer_kind({"kind": "wait_for_head", "rules": []}, _LOC) == "wait_for_head"
    assert _infer_kind({"kind": "dequeue_wake", "rules": []}, _LOC) == "dequeue_wake"


def test_engine_native_kind_rejects_file_or_command() -> None:
    with pytest.raises(LoadError, match="engine-native"):
        _infer_kind({"kind": "wait_for_head", "command": "x"}, _LOC)


def test_engine_native_kinds_have_no_typed_output_env() -> None:
    kind_args = typing.get_args(Kind)
    assert "wait_for_head" in kind_args
    assert "dequeue_wake" in kind_args
    # No typed output env for these kinds (like branch)
    assert _output_types_for_step("wait_for_head", None) is None
    assert _output_types_for_step("dequeue_wake", None) is None
