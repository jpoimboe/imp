from __future__ import annotations

import argparse

import pytest

from imp.cli.main import _select_log_events

# Two steps (spec@1-3, plan@4-6); llm_response authors carry step:<phase>/<name>
# agent_ids so agent_display renders "spec" / "plan".
_EVENTS = [
    {"id": "e1", "type": "step_started", "agent_id": None,
     "payload": {"step": "spec"}},
    {"id": "e2", "type": "llm_response", "agent_id": "step:design/spec",
     "payload": {"content": [{"type": "text", "text": "alpha"}]}},
    {"id": "e3", "type": "step_completed", "agent_id": None,
     "payload": {"step": "spec"}},
    {"id": "e4", "type": "step_started", "agent_id": None,
     "payload": {"step": "plan"}},
    {"id": "e5", "type": "llm_response", "agent_id": "step:build/plan",
     "payload": {"content": [{"type": "text", "text": "beta"}]}},
    {"id": "e6", "type": "step_completed", "agent_id": None,
     "payload": {"step": "plan"}},
]


def _ns(**kw: object) -> argparse.Namespace:
    base = {"step": None, "range": None, "range_pos": None,
            "agent": None, "limit": None}
    base.update(kw)
    return argparse.Namespace(**base)


def test_select_log_events_no_filters_is_identity() -> None:
    assert _select_log_events(_ns(), "q", _EVENTS) == _EVENTS


def test_select_log_events_step_scopes_to_window() -> None:
    out = _select_log_events(_ns(step=["spec"]), "q", _EVENTS)
    assert [e["id"] for e in out] == ["e1", "e2", "e3"]


def test_select_log_events_agent_keeps_matching_author() -> None:
    out = _select_log_events(_ns(agent=["plan"]), "q", _EVENTS)
    assert [e["id"] for e in out] == ["e5"]


def test_select_log_events_positional_range_scopes() -> None:
    out = _select_log_events(_ns(range_pos="1-2"), "q", _EVENTS)
    assert [e["id"] for e in out] == ["e1", "e2"]


def test_select_log_events_range_option_scopes() -> None:
    out = _select_log_events(_ns(range=["4-6"]), "q", _EVENTS)
    assert [e["id"] for e in out] == ["e4", "e5", "e6"]


def test_select_log_events_limit_keeps_last_n() -> None:
    out = _select_log_events(_ns(limit=2), "q", _EVENTS)
    assert [e["id"] for e in out] == ["e5", "e6"]


def test_select_log_events_unknown_step_returns_2(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = _select_log_events(_ns(step=["ghost"]), "abc", _EVENTS)
    assert rc == 2
    assert "no step 'ghost' in abc" in capsys.readouterr().err


def test_select_log_events_bad_range_returns_2(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = _select_log_events(_ns(range_pos="notanumber"), "abc", _EVENTS)
    assert rc == 2
    assert "invalid range" in capsys.readouterr().err
