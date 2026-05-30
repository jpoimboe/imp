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
    TypeEnv,
    Unary,
    VarRef,
    _tokenize,
    infer_type,
    parse,
    require_bool,
    require_type,
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


def test_parse_precedence_mul_over_add() -> None:
    # 1 + 2 * 3  ==  1 + (2 * 3)
    assert parse("1 + 2 * 3") == Binary("+", IntLit(1), Binary("*", IntLit(2), IntLit(3)))


def test_parse_and_or_precedence() -> None:
    # a or b and c  ==  a or (b and c)
    assert parse("a or b and c") == Binary(
        "or", Name("a"), Binary("and", Name("b"), Name("c"))
    )


def test_parse_not_binds_tighter_than_and() -> None:
    # not a and b  ==  (not a) and b
    assert parse("not a and b") == Binary("and", Unary("not", Name("a")), Name("b"))


def test_parse_comparison_and_arithmetic() -> None:
    # round + 1 > 3  ==  (round + 1) > 3
    assert parse("round + 1 > 3") == Binary(
        ">", Binary("+", Name("round"), IntLit(1)), IntLit(3)
    )


def test_parse_parens_override() -> None:
    assert parse("(1 + 2) * 3") == Binary("*", Binary("+", IntLit(1), IntLit(2)), IntLit(3))


def test_parse_output_and_var_refs() -> None:
    assert parse("output.major_count") == OutputRef("major_count")
    assert parse("var.round") == VarRef("round")


def test_parse_literals() -> None:
    assert parse("true") == BoolLit(True)
    assert parse("false") == BoolLit(False)
    assert parse("'rework'") == StrLit("rework")
    assert parse("-5") == Unary("-", IntLit(5))


def test_parse_equality_against_quoted_string() -> None:
    # The string form of the `if:` mapping rule {verdict: rework}
    assert parse("verdict == 'rework'") == Binary("==", Name("verdict"), StrLit("rework"))


def test_parse_chained_comparison_is_syntax_error() -> None:
    with pytest.raises(ExprSyntaxError):
        parse("1 < 2 < 3")


def test_parse_trailing_garbage_is_error() -> None:
    with pytest.raises(ExprSyntaxError):
        parse("1 2")


def test_parse_keyword_in_primary_is_error() -> None:
    with pytest.raises(ExprSyntaxError):
        parse("and")


def test_parse_unbalanced_paren_is_error() -> None:
    with pytest.raises(ExprSyntaxError):
        parse("(1 + 2")


def _env(vars: dict | None = None, output: dict | None = None) -> TypeEnv:
    return TypeEnv(vars=vars or {}, output=output)


def test_infer_literals() -> None:
    assert infer_type(parse("1"), _env()) == "int"
    assert infer_type(parse("'x'"), _env()) == "string"
    assert infer_type(parse("true"), _env()) == "bool"


def test_infer_arithmetic_is_int() -> None:
    env = _env(vars={"round": "int"})
    assert infer_type(parse("round + 1"), env) == "int"


def test_infer_comparison_is_bool() -> None:
    env = _env(vars={"round": "int"})
    assert infer_type(parse("round > 0"), env) == "bool"


def test_infer_bare_name_resolves_output_then_var() -> None:
    env = _env(vars={"review_done": "bool"}, output={"verdict": "string"})
    assert infer_type(parse("verdict == 'approve'"), env) == "bool"
    assert infer_type(parse("review_done"), env) == "bool"


def test_infer_ambiguous_bare_name_is_error() -> None:
    env = _env(vars={"verdict": "string"}, output={"verdict": "string"})
    with pytest.raises(ExprTypeError):
        infer_type(parse("verdict"), env)


def test_infer_unknown_identifier_is_error() -> None:
    with pytest.raises(ExprTypeError):
        infer_type(parse("nope"), _env(vars={"round": "int"}))


def test_infer_output_ref_in_branch_is_error() -> None:
    # branch: output is None
    with pytest.raises(ExprTypeError):
        infer_type(parse("output.x"), _env(vars={"x": "int"}, output=None))


def test_infer_arithmetic_on_non_int_is_error() -> None:
    env = _env(vars={"name": "string"})
    with pytest.raises(ExprTypeError):
        infer_type(parse("name + 1"), env)


def test_infer_equality_type_mismatch_is_error() -> None:
    env = _env(vars={"round": "int"})
    with pytest.raises(ExprTypeError):
        infer_type(parse("round == 'x'"), env)


def test_infer_and_requires_bool() -> None:
    env = _env(vars={"round": "int"})
    with pytest.raises(ExprTypeError):
        infer_type(parse("round and true"), env)


def test_require_bool_rejects_non_bool() -> None:
    env = _env(vars={"round": "int"})
    require_bool(parse("round > 0"), env)        # ok
    with pytest.raises(ExprTypeError):
        require_bool(parse("round"), env)        # `if: round` is illegal


def test_require_type_checks_set_target() -> None:
    env = _env(vars={"round": "int"})
    require_type(parse("round + 1"), env, "int")        # ok
    with pytest.raises(ExprTypeError):
        require_type(parse("true"), env, "int")          # bool into int var
