"""Shared complete_step output-validation decision point (Ok | Nudge | LoopError)."""

from __future__ import annotations

from pathlib import Path

from imp.agents.loop_guard import DEFAULT_NOPROGRESS_N, LoopGuard
from imp.agents.output_guard import (
    Nudge,
    Ok,
    RecoveryInfo,
    build_nudge_message,
    make_recovery_event,
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


def test_validate_recovers_mirror_fold_returns_ok_with_metadata() -> None:
    g = LoopGuard()
    folded = (
        "BODY</summary>\n"
        '<parameter name="outcome">did the thing</outcome>\n</invoke>'
    )
    res = validate_output_or_nudge({"summary": folded}, _recovery_schema(), g)
    assert isinstance(res, Ok)
    assert res.payload == {"summary": "BODY", "outcome": "did the thing"}
    assert res.recovered is not None
    assert res.recovered.container == "summary"
    assert res.recovered.fields == ("outcome",)
    assert g.noprogress_streak == 0


def test_validate_lone_mirror_tag_still_nudges() -> None:
    g = LoopGuard()
    args = {"summary": "text </summary> more text, no param opener"}
    res = validate_output_or_nudge(args, _recovery_schema(), g)
    assert isinstance(res, Nudge)
    assert "outcome" in res.reason


def test_make_recovery_event_names_container_and_fields() -> None:
    ok = Ok(
        payload={"summary": "b", "outcome": "x", "status": "Success"},
        recovered=RecoveryInfo(container="summary", fields=("outcome", "status")),
    )
    ev = make_recovery_event(ok)
    assert ev == {
        "type": "output_recovered",
        "payload": {"container": "summary", "fields": ["outcome", "status"]},
    }


def test_validate_partial_recovery_is_discarded_whole() -> None:
    schema = parse_output_schema(
        {
            "summary": {"type": "string"},
            "outcome": {"type": "string"},
            "note": {"type": "string"},
        },
        source=SourceLoc(file=Path("a.yaml"), line=1),
    )
    g = LoopGuard()
    folded = "BODY</summary>\n<parameter name=\"outcome\">x</outcome>\n</invoke>"
    res = validate_output_or_nudge({"summary": folded}, schema, g)
    assert isinstance(res, Nudge)  # 'note' still missing -> atomic discard -> nudge
    assert g.noprogress_streak == 1


def _gili38_schema():
    # task-lead schema (with_outcome_field applied): every field required.
    raw = {
        "status": {"type": "enum", "values": ["Success", "Error"]},
        "verify_tolerated": {"type": "bool"},
        "summary": {"type": "string"},
        "outcome": {"type": "string"},
    }
    return parse_output_schema(raw, source=SourceLoc(file=Path("a.yaml"), line=1))


def _reviewer_schema():
    # patent-reviewer schema (with_outcome_field applied): every field required.
    raw = {
        "status": {"type": "enum", "values": ["Success", "Error"]},
        "findings": {"type": "string"},
        "blocking": {"type": "bool"},
        "has_minor_findings": {"type": "bool"},
        "outcome": {"type": "string"},
    }
    return parse_output_schema(raw, source=SourceLoc(file=Path("a.yaml"), line=1))


def test_recover_gili38_flat_routing_field_fold() -> None:
    # Real gili38 task-lead fold: the required enum `status` was folded into the
    # tail of `outcome`, which mirror-closes `</outcome>` then carries an
    # unclosed `<parameter name="status">Success` running to end-of-string.
    g = LoopGuard()
    args = {
        "outcome": (
            "Completed all 4 tasks of plan-3. Proceeding to verify gate."
            '</outcome>\n<parameter name="status">Success'
        ),
        "verify_tolerated": False,
        "summary": "Implemented Plan 3 end to end; tree clean.",
    }
    res = validate_output_or_nudge(args, _gili38_schema(), g)
    assert isinstance(res, Ok)
    assert res.payload["status"] == "Success"
    assert res.payload["outcome"] == (
        "Completed all 4 tasks of plan-3. Proceeding to verify gate."
    )
    assert res.recovered is not None
    assert res.recovered.container == "outcome"
    assert res.recovered.fields == ("status",)
    assert g.noprogress_streak == 0


def test_recover_kibe87_findings_folds_outcome_mirror_close() -> None:
    # Dominant real reviewer shape: `findings` mirror-closes `</findings>` then
    # folds a `<parameter name="outcome">...</outcome>` block (mirror close).
    g = LoopGuard()
    args = {
        "status": "Success",
        "findings": (
            "No blocking issues; the design is sound."
            '</findings>\n<parameter name="outcome">reviewed clean, minors '
            "noted</outcome>\n</invoke>"
        ),
        "blocking": False,
        "has_minor_findings": True,
    }
    res = validate_output_or_nudge(args, _reviewer_schema(), g)
    assert isinstance(res, Ok)
    assert res.payload["findings"] == "No blocking issues; the design is sound."
    assert res.payload["outcome"] == "reviewed clean, minors noted"
    assert res.recovered is not None
    assert res.recovered.container == "findings"
    assert res.recovered.fields == ("outcome",)
    assert g.noprogress_streak == 0


def test_recover_kibe87_findings_folds_blocking_and_outcome() -> None:
    # Multi-field fold: `findings` folds BOTH a `blocking` bool and an `outcome`
    # string; both are recovered and `blocking` is type-coerced "false" -> False.
    g = LoopGuard()
    args = {
        "status": "Success",
        "findings": (
            "Two majors and a minor."
            '</findings>\n<parameter name="blocking">false</parameter>\n'
            '<parameter name="outcome">sent back for rework</outcome>\n</invoke>'
        ),
        "has_minor_findings": True,
    }
    res = validate_output_or_nudge(args, _reviewer_schema(), g)
    assert isinstance(res, Ok)
    assert res.payload["findings"] == "Two majors and a minor."
    assert res.payload["blocking"] is False
    assert res.payload["outcome"] == "sent back for rework"
    assert res.recovered is not None
    assert res.recovered.container == "findings"
    assert res.recovered.fields == ("blocking", "outcome")
    assert g.noprogress_streak == 0


def test_recover_terminator_precedence_cuts_at_earliest() -> None:
    # A folded value carrying ALL THREE terminators (`</outcome>`, `</parameter>`,
    # `</invoke>`) is cut at the EARLIEST one. Here `</outcome>` precedes the
    # others, so the value is "VAL" — a naive `</parameter>`-only cut would wrongly
    # yield "VAL</outcome>junk".
    args = {
        "summary": (
            'B</summary>\n<parameter name="outcome">VAL</outcome>junk'
            "</parameter>more</invoke>tail"
        )
    }
    repaired = recover_folded_fields(args, _recovery_schema())
    assert repaired == {"summary": "B", "outcome": "VAL"}


def test_recover_coerces_bool_false() -> None:
    schema = parse_output_schema(
        {"findings": {"type": "string"}, "blocking": {"type": "bool"}},
        source=SourceLoc(file=Path("a.yaml"), line=1),
    )
    folded = 'x</findings>\n<parameter name="blocking">false</parameter>\n</invoke>'
    repaired = recover_folded_fields({"findings": folded}, schema)
    assert repaired["blocking"] is False


def test_recover_non_coercible_value_left_missing() -> None:
    # A folded `blocking` value of "maybe" is not a bool: it is NOT injected, so
    # the field stays missing in the repaired copy.
    schema = parse_output_schema(
        {"summary": {"type": "string"}, "blocking": {"type": "bool"}},
        source=SourceLoc(file=Path("a.yaml"), line=1),
    )
    folded = 'B</summary>\n<parameter name="blocking">maybe</parameter>\n</invoke>'
    repaired = recover_folded_fields({"summary": folded}, schema)
    assert repaired == {"summary": "B"}


def test_validate_non_coercible_fold_falls_to_nudge() -> None:
    # Same non-coercible fold at the decision-point level: the still-missing
    # required `blocking` fails re-validation -> atomic discard -> nudge.
    schema = parse_output_schema(
        {"summary": {"type": "string"}, "blocking": {"type": "bool"}},
        source=SourceLoc(file=Path("a.yaml"), line=1),
    )
    g = LoopGuard()
    folded = 'B</summary>\n<parameter name="blocking">maybe</parameter>\n</invoke>'
    res = validate_output_or_nudge({"summary": folded}, schema, g)
    assert isinstance(res, Nudge)
    assert "blocking" in res.reason
    assert g.noprogress_streak == 1


def test_clean_value_bails_beyond_depth_bound() -> None:
    # Direct contract test of the depth guard: within budget the mirror-close
    # chased by a param opener is truncated; beyond the budget it bails with None
    # so the caller skips the field rather than recursing without bound.
    from imp.agents.output_guard import _MAX_FOLD_DEPTH, _clean_value

    nested = 'real</x>\n<parameter name="z">tail'
    assert _clean_value(nested, "x", 0) == "real"
    assert _clean_value(nested, "x", _MAX_FOLD_DEPTH + 1) is None


def test_recover_json_without_param_markers_is_unchanged() -> None:
    # OpenAI/codex JSON path: args carry no `<parameter name="...">` markers, so
    # the fold fingerprint never matches and the args pass through unchanged.
    schema = parse_output_schema(
        {"status": {"type": "enum", "values": ["Success", "Error"]},
         "note": {"type": "string"}},
        source=SourceLoc(file=Path("a.yaml"), line=1),
    )
    args = {"status": "Success", "note": "plain narrative, no tags at all"}
    assert recover_folded_fields(args, schema) == args
