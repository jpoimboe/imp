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
