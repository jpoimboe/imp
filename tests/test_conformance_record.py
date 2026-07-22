from __future__ import annotations

from types import SimpleNamespace

import pytest

from imp.engine.state import InMemoryEventLog
from tests import conformance_record as cr


def _driven(control, log_events, raised=None):
    log = InMemoryEventLog()
    for e in log_events:
        log._events.append(e)  # direct seed: bypass schema validation for the unit test
    return SimpleNamespace(control=control, log=log, raised=raised)


def test_record_projects_stable_fields() -> None:
    driven = _driven(
        control=[
            {"type": "agent_result", "payload": {"status": "Success", "note": "ok"}},
        ],
        log_events=[
            {"type": "step_context", "payload": {}},
            {"type": "engine_message", "payload": {}},
            {"type": "agent_message", "payload": {"usage": {"in": 5}, "duration_ms": 12}},
            {"type": "tool_call", "payload": {"id": "x"}},
        ],
    )
    rec = cr.record(driven)
    assert rec["terminal"] == "agent_result"
    assert rec["control_types"] == ["agent_result"]
    assert rec["agent_result"] == {"status": "Success", "note": "ok"}
    assert rec["errors"] == []
    assert rec["log_types"] == [
        "step_context", "engine_message", "agent_message", "tool_call"]
    assert rec["step_context_count"] == 1
    assert rec["raised"] is None
    # Volatile fields (usage/duration) never enter the record.
    assert "usage" not in repr(rec) and "duration_ms" not in repr(rec)


def test_record_extracts_error_category_kind_and_raised() -> None:
    driven = _driven(
        control=[{"type": "error",
                  "payload": {"category": "config", "kind": "tool_call_ceiling_exceeded",
                              "message": "ceiling hit at 2", "detail": {"n": 2}}}],
        log_events=[],
        raised=RuntimeError("boom"),
    )
    rec = cr.record(driven)
    assert rec["errors"] == [["config", "tool_call_ceiling_exceeded"]]
    assert rec["terminal"] == "error"
    assert rec["agent_result"] is None
    assert rec["raised"] == "RuntimeError"


def test_check_golden_writes_then_matches(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cr, "GOLDEN_DIR", tmp_path / "golden")
    rec = {"terminal": "agent_result", "log_types": ["step_context"]}
    # Update mode writes the file and returns without asserting.
    monkeypatch.setenv("IMP_CONFORMANCE_UPDATE", "1")
    cr.check_golden("demo", rec)
    assert (tmp_path / "golden" / "demo.json").exists()
    # Compare mode: identical record passes, a changed record fails.
    monkeypatch.delenv("IMP_CONFORMANCE_UPDATE", raising=False)
    cr.check_golden("demo", rec)
    with pytest.raises(AssertionError):
        cr.check_golden("demo", {"terminal": "error", "log_types": []})


def test_check_golden_missing_file_fails(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cr, "GOLDEN_DIR", tmp_path / "golden")
    monkeypatch.delenv("IMP_CONFORMANCE_UPDATE", raising=False)
    with pytest.raises(AssertionError):
        cr.check_golden("absent", {"terminal": None})
