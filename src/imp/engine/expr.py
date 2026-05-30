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


# ── parser ──────────────────────────────────────────────────────────────────

_CMP = {"==", "!=", "<", "<=", ">", ">="}


class _Parser:
    def __init__(self, toks: list[_Tok], text: str) -> None:
        self._toks = toks
        self._i = 0
        self._text = text

    def _peek(self) -> _Tok:
        return self._toks[self._i]

    def _advance(self) -> _Tok:
        t = self._toks[self._i]
        self._i += 1
        return t

    def _expect(self, kind: str) -> _Tok:
        t = self._peek()
        if t.kind != kind:
            raise ExprSyntaxError(
                f"expected {kind}, got {t.kind} {t.value!r} in {self._text!r}"
            )
        return self._advance()

    def _is_keyword(self, word: str) -> bool:
        t = self._peek()
        return t.kind == "NAME" and t.value == word

    def parse(self) -> Expr:
        e = self._or()
        if self._peek().kind != "EOF":
            t = self._peek()
            raise ExprSyntaxError(f"unexpected trailing {t.value!r} in {self._text!r}")
        return e

    def _or(self) -> Expr:
        left = self._and()
        while self._is_keyword("or"):
            self._advance()
            left = Binary("or", left, self._and())
        return left

    def _and(self) -> Expr:
        left = self._not()
        while self._is_keyword("and"):
            self._advance()
            left = Binary("and", left, self._not())
        return left

    def _not(self) -> Expr:
        if self._is_keyword("not"):
            self._advance()
            return Unary("not", self._not())
        return self._comparison()

    def _comparison(self) -> Expr:
        left = self._add()
        t = self._peek()
        if t.kind == "OP" and t.value in _CMP:
            self._advance()
            right = self._add()
            t2 = self._peek()
            if t2.kind == "OP" and t2.value in _CMP:
                raise ExprSyntaxError(
                    f"chained comparison not allowed in {self._text!r}"
                )
            return Binary(t.value, left, right)
        return left

    def _add(self) -> Expr:
        left = self._mul()
        while self._peek().kind == "OP" and self._peek().value in ("+", "-"):
            op = self._advance().value
            left = Binary(op, left, self._mul())
        return left

    def _mul(self) -> Expr:
        left = self._unary()
        while self._peek().kind == "OP" and self._peek().value in ("*", "/"):
            op = self._advance().value
            left = Binary(op, left, self._unary())
        return left

    def _unary(self) -> Expr:
        t = self._peek()
        if t.kind == "OP" and t.value == "-":
            self._advance()
            return Unary("-", self._unary())
        return self._primary()

    def _primary(self) -> Expr:
        t = self._peek()
        if t.kind == "INT":
            self._advance()
            return IntLit(int(t.value))
        if t.kind == "STR":
            self._advance()
            return StrLit(t.value)
        if t.kind == "LPAREN":
            self._advance()
            e = self._or()
            self._expect("RPAREN")
            return e
        if t.kind == "NAME":
            self._advance()
            if t.value in ("true", "false"):
                return BoolLit(t.value == "true")
            if t.value in ("and", "or", "not"):
                raise ExprSyntaxError(
                    f"unexpected keyword {t.value!r} in {self._text!r}"
                )
            if t.value in ("output", "var") and self._peek().kind == "DOT":
                self._advance()  # consume DOT
                field = self._expect("NAME").value
                return OutputRef(field) if t.value == "output" else VarRef(field)
            return Name(t.value)
        raise ExprSyntaxError(
            f"unexpected token {t.value!r} ({t.kind}) in {self._text!r}"
        )


def parse(text: str) -> Expr:
    """Tokenize + parse a string expression into an AST. Raises ExprSyntaxError."""
    return _Parser(_tokenize(text), text).parse()
