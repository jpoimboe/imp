# src/imp/engine/expr.py
"""Closed expression language for workflow rules (`if:` / `set:`).

No eval, no function calls, no indexing beyond output.<field> / var.<name>.
Three independent stages, each usable on its own:
  parse(text)            -> Expr           (syntax)
  infer_type(expr, env)  -> ExprType       (static type-check; run at load)
  evaluate(expr, env)    -> int|bool|str   (runtime)

The loader builds AST nodes for the `if:` mapping form directly
(Binary("==", Name(k), <literal>)); the string form goes through parse().
Both then share infer_type / evaluate. This module raises its own ExprError
subclasses; the loader maps them to LoadError with a YAML SourceLoc, so expr.py
has zero imports from the rest of imp.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ExprType = Literal["int", "bool", "string"]


# ── errors ──────────────────────────────────────────────────────────────────

class ExprError(Exception):
    """Base for all expression errors."""


class ExprSyntaxError(ExprError):
    """Tokenizer or parser could not read the expression text."""


class ExprTypeError(ExprError):
    """Static type-check failed (load time)."""


class ExprEvalError(ExprError):
    """Runtime evaluation failed (e.g. division by zero)."""


# ── AST ─────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class Expr:
    """Base for all AST nodes. Use match/case to dispatch."""


@dataclass(frozen=True, slots=True)
class IntLit(Expr):
    value: int


@dataclass(frozen=True, slots=True)
class StrLit(Expr):
    value: str


@dataclass(frozen=True, slots=True)
class BoolLit(Expr):
    value: bool


@dataclass(frozen=True, slots=True)
class Name(Expr):
    """A bare identifier — resolved per-step against output fields and vars."""
    name: str


@dataclass(frozen=True, slots=True)
class OutputRef(Expr):
    """`output.<field>` — explicit reference to this step's output."""
    field: str


@dataclass(frozen=True, slots=True)
class VarRef(Expr):
    """`var.<name>` — explicit reference to a workflow var."""
    name: str


@dataclass(frozen=True, slots=True)
class Unary(Expr):
    op: str          # "-" | "not"
    operand: Expr


@dataclass(frozen=True, slots=True)
class Binary(Expr):
    op: str          # + - * /  ==  !=  <  <=  >  >=  and  or
    left: Expr
    right: Expr


# ── tokenizer ───────────────────────────────────────────────────────────────

_TWO_CHAR = {"==", "!=", "<=", ">="}


@dataclass(frozen=True, slots=True)
class _Tok:
    kind: str   # INT STR NAME OP DOT LPAREN RPAREN EOF
    value: str


def _tokenize(text: str) -> list[_Tok]:
    toks: list[_Tok] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c.isspace():
            i += 1
            continue
        if c in "'\"":
            quote = c
            j = i + 1
            buf: list[str] = []
            while j < n and text[j] != quote:
                buf.append(text[j])
                j += 1
            if j >= n:
                raise ExprSyntaxError(f"unterminated string literal in {text!r}")
            toks.append(_Tok("STR", "".join(buf)))
            i = j + 1
            continue
        if c.isdigit():
            j = i
            while j < n and text[j].isdigit():
                j += 1
            toks.append(_Tok("INT", text[i:j]))
            i = j
            continue
        if c.isalpha() or c == "_":
            j = i
            while j < n and (text[j].isalnum() or text[j] == "_"):
                j += 1
            toks.append(_Tok("NAME", text[i:j]))
            i = j
            continue
        two = text[i : i + 2]
        if two in _TWO_CHAR:
            toks.append(_Tok("OP", two))
            i += 2
            continue
        if c == "(":
            toks.append(_Tok("LPAREN", c))
            i += 1
            continue
        if c == ")":
            toks.append(_Tok("RPAREN", c))
            i += 1
            continue
        if c == ".":
            toks.append(_Tok("DOT", c))
            i += 1
            continue
        if c in "+-*/<>":
            toks.append(_Tok("OP", c))
            i += 1
            continue
        raise ExprSyntaxError(f"unexpected character {c!r} in {text!r}")
    toks.append(_Tok("EOF", ""))
    return toks
