"""Shared complete_step output-validation decision point (Ok | Nudge | LoopError)."""

from __future__ import annotations

from pathlib import Path

from imp.agents.loop_guard import DEFAULT_NOPROGRESS_N, LoopGuard
from imp.agents.output_guard import (
    Nudge,
    Ok,
    build_nudge_message,
    recover_folded_fields,
    validate_output_or_nudge,
)
from imp.engine.schema import parse_output_schema
from imp.engine.types import SourceLoc
from imp.providers.errors import LoopError


def _schema():
    raw = {
        "status": {"type": "enum", "values": ["Success", "Error"]},
        "blocking": {"type": "bool"},
    }
    return parse_output_schema(raw, source=SourceLoc(file=Path("a.yaml"), line=1))


def test_valid_args_returns_ok_and_leaves_guard_untouched() -> None:
    g = LoopGuard()
    res = validate_output_or_nudge(
        {"status": "Success", "blocking": True}, _schema(), g)
    assert isinstance(res, Ok)
    assert g.noprogress_streak == 0


def test_missing_required_field_returns_nudge_naming_the_field() -> None:
    g = LoopGuard()
    res = validate_output_or_nudge({"status": "Success"}, _schema(), g)
    assert isinstance(res, Nudge)
    assert "blocking" in res.reason
    assert "missing required field" in res.reason
    assert "complete_step" in res.message


def test_wrong_type_returns_nudge_naming_the_field_and_type() -> None:
    g = LoopGuard()
    res = validate_output_or_nudge(
        {"status": "Success", "blocking": "yes"}, _schema(), g)
    assert isinstance(res, Nudge)
    assert "blocking" in res.reason
    assert "bool" in res.reason


def test_nudge_attempt_counts_consecutive_failures() -> None:
    g = LoopGuard()
    schema = _schema()
    n1 = validate_output_or_nudge({"status": "Success"}, schema, g)
    n2 = validate_output_or_nudge({"status": "Success"}, schema, g)
    assert isinstance(n1, Nudge) and n1.attempt == 1
    assert isinstance(n2, Nudge) and n2.attempt == 2


def test_repeated_identical_failure_trips_repeated_failure() -> None:
    g = LoopGuard()
    schema = _schema()
    assert isinstance(validate_output_or_nudge({"status": "Success"}, schema, g), Nudge)
    assert isinstance(validate_output_or_nudge({"status": "Success"}, schema, g), Nudge)
    res = validate_output_or_nudge({"status": "Success"}, schema, g)
    assert isinstance(res, LoopError)
    assert getattr(res, "kind", "") == "repeated_failure"


def test_distinct_field_failures_trip_no_progress() -> None:
    g = LoopGuard()
    schema = _schema()
    # Alternate two invalid args with DISTINCT validation reasons: missing
    # 'status' vs missing 'blocking'. Distinct signatures never reach K=3
    # identical, but no-progress accumulates and trips at N.
    variants = [{"blocking": True}, {"status": "Success"}]
    last: object = None
    for i in range(DEFAULT_NOPROGRESS_N):
        last = validate_output_or_nudge(variants[i % 2], schema, g)
    assert isinstance(last, LoopError)
    assert getattr(last, "kind", "") == "no_progress"


def test_valid_call_after_failures_returns_ok_not_looperror() -> None:
    g = LoopGuard()
    schema = _schema()
    validate_output_or_nudge({"status": "Success"}, schema, g)  # nudge 1
    validate_output_or_nudge({"status": "Success"}, schema, g)  # nudge 2
    res = validate_output_or_nudge(
        {"status": "Success", "blocking": True}, schema, g)
    assert isinstance(res, Ok)


def test_build_nudge_message_is_deterministic_and_names_reason() -> None:
    reason = "missing required field 'blocking'"
    msg = build_nudge_message(reason)
    assert msg == build_nudge_message(reason)  # stable, no nonce/timestamp
    assert reason in msg
    assert "complete_step" in msg


def test_make_nudge_event_carries_reason_and_attempt() -> None:
    from imp.agents.output_guard import make_nudge_event

    nudge = Nudge(
        message="Your complete_step call was invalid: ...",
        reason="missing required field 'blocking'",
        attempt=2,
    )
    ev = make_nudge_event(nudge)
    assert ev["type"] == "output_validation_nudge"
    assert ev["payload"] == {
        "message": "missing required field 'blocking'",
        "attempt": 2,
    }


def test_valid_args_returns_ok_with_payload_and_no_recovery() -> None:
    g = LoopGuard()
    args = {"status": "Success", "blocking": True}
    res = validate_output_or_nudge(args, _schema(), g)
    assert isinstance(res, Ok)
    assert res.payload == args
    assert res.recovered is None


def _recovery_schema():
    raw = {
        "summary": {"type": "string"},
        "outcome": {"type": "string"},
    }
    return parse_output_schema(raw, source=SourceLoc(file=Path("a.yaml"), line=1))


def test_recover_folded_fields_recovers_mirror_closed_outcome() -> None:
    folded = (
        "SUMMARY BODY</summary>\n"
        '<parameter name="outcome">wrote plan-1</outcome>\n</invoke>'
    )
    repaired = recover_folded_fields({"summary": folded}, _recovery_schema())
    assert repaired == {"summary": "SUMMARY BODY", "outcome": "wrote plan-1"}


def test_recover_folded_fields_recovers_parameter_closed_outcome() -> None:
    folded = (
        "SUMMARY BODY</summary>\n"
        '<parameter name="outcome">wrote plan-1</parameter>\n</invoke>'
    )
    repaired = recover_folded_fields({"summary": folded}, _recovery_schema())
    assert repaired == {"summary": "SUMMARY BODY", "outcome": "wrote plan-1"}


def test_recover_folded_fields_ignores_lone_mirror_tag() -> None:
    args = {"summary": "we compared </summary> semantics; nothing folded here"}
    repaired = recover_folded_fields(args, _recovery_schema())
    assert repaired == args
