# tests/test_expr.py
from __future__ import annotations

import pytest

from imp.engine.expr import (
    Binary,
    BoolLit,
    ExprError,
    ExprEvalError,
    ExprSyntaxError,
    ExprTypeError,
    IntLit,
    Name,
    OutputRef,
    StrLit,
    Unary,
    VarRef,
)


def test_ast_nodes_construct_and_are_frozen() -> None:
    node = Binary("==", Name("verdict"), StrLit("rework"))
    assert node.op == "=="
    assert node.left == Name("verdict")
    assert node.right == StrLit("rework")
    # frozen dataclass: assignment raises
    with pytest.raises(AttributeError):
        node.op = "!="  # type: ignore[misc]


def test_ast_equality_is_structural() -> None:
    assert IntLit(3) == IntLit(3)
    assert OutputRef("major_count") == OutputRef("major_count")
    assert VarRef("round") != VarRef("rounds")
    assert Unary("not", BoolLit(True)) == Unary("not", BoolLit(True))


def test_error_hierarchy() -> None:
    for cls in (ExprSyntaxError, ExprTypeError, ExprEvalError):
        assert issubclass(cls, ExprError)
