"""Shared decision point for a malformed top-level complete_step.

Returns Ok (args valid), Nudge (recoverable — provider delivers `message`
in-session so the agent re-emits), or LoopError (the invocation's LoopGuard
tripped — provider surfaces it as pause-to-human). The validation-failed turn is
fed to the guard EXACTLY ONCE here, so a provider routing through this helper must
NOT separately observe() the same turn.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypedDict

from imp.agents.loop_guard import LoopGuard, TurnRecord
from imp.engine.errors import LoadError
from imp.engine.schema import validate_agent_output
from imp.engine.types import OutputField, OutputSchema

if TYPE_CHECKING:
    from imp.providers.errors import LoopError


@dataclass(frozen=True)
class RecoveryInfo:
    """Metadata for a fired fold-recovery: the container field whose value was
    over-read and the field names un-folded out of it. Read by the
    output_recovered event."""
    container: str
    fields: tuple[str, ...]


@dataclass(frozen=True)
class Ok:
    """complete_step args validated (possibly after fold-recovery); the provider
    yields `payload`. `recovered` is None on a clean pass-through, non-None when a
    repair fired."""
    payload: dict[str, Any]
    recovered: RecoveryInfo | None = None


@dataclass(frozen=True)
class Nudge:
    """A recoverable output-validation failure to deliver back to the agent."""
    message: str   # in-session text delivered to the agent to prompt a re-emit
    reason: str    # stable validation-error core (guard signature + event payload)
    attempt: int   # consecutive-failure streak, for the observability event


def build_nudge_message(reason: str) -> str:
    """Deterministic single-sourced nudge text (stable, no nonce, one guard signature)."""
    return (
        f"Your complete_step call was invalid: {reason}. "
        "Call complete_step again with all required fields present and correctly "
        "typed."
    )


_PARAM_OPEN = re.compile(r'<parameter\s+name="([^"]+)">')
_PARAM_OPEN_PREFIX = re.compile(r'\s*<parameter\s+name="')
_MAX_FOLD_DEPTH = 2
_COERCE_FAIL = object()


def _earliest_terminator(tail: str, start: int, y: str) -> int:
    """Index where field `y`'s folded value ends: the earliest of a clean
    `</parameter>`, the param's own mirror close `</y>`, `</invoke>`, or
    end-of-string."""
    cands = [
        tail.find("</parameter>", start),
        tail.find(f"</{y}>", start),
        tail.find("</invoke>", start),
    ]
    ends = [c for c in cands if c != -1]
    return min(ends) if ends else len(tail)


def _clean_value(raw: str, name: str, depth: int) -> str | None:
    """A recovered value may itself carry a further fold (its own `</name>`
    chased by a `<parameter name=`). Truncate at that mirror close, bounded to
    _MAX_FOLD_DEPTH; beyond the bound, return None so the caller skips the field
    (it stays missing and the call falls to the nudge)."""
    if depth > _MAX_FOLD_DEPTH:
        return None
    close = f"</{name}>"
    ci = raw.find(close)
    if ci != -1 and _PARAM_OPEN_PREFIX.match(raw, ci + len(close)):
        return _clean_value(raw[:ci], name, depth + 1)
    return raw


def _coerce(raw: str, field: OutputField) -> Any:
    """Coerce a folded string value to the field's declared type. Returns
    _COERCE_FAIL on any non-coercible value so the caller leaves the field
    missing."""
    v = raw.strip()
    if field.type == "bool":
        low = v.lower()
        if low == "true":
            return True
        if low == "false":
            return False
        return _COERCE_FAIL
    if field.type == "int":
        try:
            return int(v)
        except ValueError:
            return _COERCE_FAIL
    if field.type == "enum":
        return v if v in field.values else _COERCE_FAIL
    if field.type == "string":
        return v
    return _COERCE_FAIL


def recover_folded_fields(
    args: dict[str, Any], schema: OutputSchema
) -> dict[str, Any]:
    """Return a repaired COPY of `args` with decoder-folded fields un-folded.

    For any string field whose value carries the fold fingerprint — the earliest
    close tag (the field's own name-mirror close `</c>` OR a generic
    `</parameter>`) that is immediately followed (after optional whitespace) by a
    `<parameter name="...">` opener — the container's true value is the text
    before that close tag, and each `<parameter name="Y">` in the tail is
    extracted up to the earliest terminator and injected as Y (only if Y is a
    declared field currently absent or null, coerced to Y's type). Never mutates
    `args`."""
    declared = {f.name: f for f in schema.fields}
    repaired = dict(args)
    for name, value in args.items():
        if not isinstance(value, str):
            continue
        boundary = re.compile(
            r'</(?:parameter|' + re.escape(name) + r')>\s*(?=<parameter\s+name=")'
        )
        m = boundary.search(value)
        if m is None:
            continue
        repaired[name] = value[: m.start()]
        tail = value[m.end():]
        for pm in _PARAM_OPEN.finditer(tail):
            y = pm.group(1)
            end = _earliest_terminator(tail, pm.end(), y)
            cleaned = _clean_value(tail[pm.end():end], y, 0)
            if cleaned is None:
                continue
            field = declared.get(y)
            if field is None:
                continue
            if repaired.get(y) is not None:
                continue
            coerced = _coerce(cleaned, field)
            if coerced is _COERCE_FAIL:
                continue
            repaired[y] = coerced
    return repaired


def _recovery_info(original: dict[str, Any], repaired: dict[str, Any]) -> RecoveryInfo:
    container = next(
        k for k, v in original.items() if k in repaired and repaired[k] != v
    )
    fields = tuple(
        k for k in repaired
        if original.get(k) is None and repaired.get(k) is not None
    )
    return RecoveryInfo(container=container, fields=fields)


def validate_output_or_nudge(
    args: dict[str, Any],
    schema: OutputSchema,
    guard: LoopGuard,
) -> Ok | Nudge | LoopError:
    """Validate a top-level complete_step's args; on failure, feed the guard once
    and decide nudge-vs-pause."""
    try:
        validate_agent_output(args, schema)
    except LoadError as e:
        if any(f.required and f.name not in args for f in schema.fields):
            repaired = recover_folded_fields(args, schema)
            if repaired != args:
                try:
                    validate_agent_output(repaired, schema)
                except LoadError:
                    pass
                else:
                    return Ok(
                        payload=repaired,
                        recovered=_recovery_info(args, repaired),
                    )
        reason = e.message
        record = TurnRecord(
            assistant_text="",
            tools=[],
            has_complete_step=False,
            error_category="agent_output_missing",
            error_kind="output_validation",
            error_message=reason,
        )
        loop_err = guard.observe(record)
        if loop_err is not None:
            return loop_err
        return Nudge(
            message=build_nudge_message(reason),
            reason=reason,
            attempt=guard.noprogress_streak,
        )
    return Ok(payload=args, recovered=None)


class NudgeEvent(TypedDict):
    type: str
    payload: dict[str, object]


def make_nudge_event(nudge: Nudge) -> NudgeEvent:
    """Single-sourced nudge event payload, emitted identically by every provider."""
    return {
        "type": "output_validation_nudge",
        "payload": {"message": nudge.reason, "attempt": nudge.attempt},
    }


class RecoveryEvent(TypedDict):
    type: str
    payload: dict[str, object]


def make_recovery_event(ok: Ok) -> RecoveryEvent:
    """Single-sourced output_recovered event payload, emitted identically by
    every provider when a fold-recovery fired (ok.recovered is not None)."""
    assert ok.recovered is not None
    return {
        "type": "output_recovered",
        "payload": {
            "container": ok.recovered.container,
            "fields": list(ok.recovered.fields),
        },
    }
