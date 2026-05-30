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
    _tokenize,
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


def _kinds(text: str) -> list[tuple[str, str]]:
    return [(t.kind, t.value) for t in _tokenize(text)]


def test_tokenize_numbers_names_ops() -> None:
    assert _kinds("round + 1") == [
        ("NAME", "round"), ("OP", "+"), ("INT", "1"), ("EOF", ""),
    ]


def test_tokenize_two_char_ops() -> None:
    assert _kinds("a == b") == [
        ("NAME", "a"), ("OP", "=="), ("NAME", "b"), ("EOF", ""),
    ]
    assert [t.value for t in _tokenize("x>=1 and y<=2")][:-1] == [
        "x", ">=", "1", "and", "y", "<=", "2",
    ]


def test_tokenize_string_literal() -> None:
    assert _kinds("'rework'") == [("STR", "rework"), ("EOF", "")]
    assert _kinds('"approve"') == [("STR", "approve"), ("EOF", "")]


def test_tokenize_dotted_and_parens() -> None:
    assert _kinds("output.major_count") == [
        ("NAME", "output"), ("DOT", "."), ("NAME", "major_count"), ("EOF", ""),
    ]
    assert [t.kind for t in _tokenize("(a)")] == ["LPAREN", "NAME", "RPAREN", "EOF"]


def test_tokenize_unterminated_string_raises() -> None:
    with pytest.raises(ExprSyntaxError):
        _tokenize("'oops")


def test_tokenize_unexpected_char_raises() -> None:
    with pytest.raises(ExprSyntaxError):
        _tokenize("a % b")  # '%' is deferred / unsupported
