from __future__ import annotations

import json
from pathlib import Path

from imp.cli import narrative as nv
from imp.cli.render_text import render_tree
from imp.engine.state import JsonlEventLog
from imp.providers.quota import QuotaReading, QuotaWindow
from tests._event_fixtures import SAMPLE, ev

_RAW = {
    "limit_id": "codex", "limit_name": None,
    "primary": {"used_percent": 12.0, "window_minutes": 300, "resets_at": 1788403732},
    "secondary": {"used_percent": 14.0, "window_minutes": 10080, "resets_at": 1788971433},
    "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
    "individual_limit": None, "spend_control_reached": None,
    "plan_type": "plus", "rate_limit_reached_type": None,
}


def _reading() -> QuotaReading:
    return QuotaReading(
        provider="codex", plan="plus",
        windows=(QuotaWindow(label="5h", used_pct=12.0, resets_at=1788403732),
                 QuotaWindow(label="7d", used_pct=14.0, resets_at=1788971433)),
        raw=_RAW)


def test_to_payload_projects_windows_in_order_and_carries_raw_verbatim() -> None:
    assert _reading().to_payload() == {
        "provider": "codex",
        "plan": "plus",
        "windows": [{"label": "5h", "used_pct": 12.0, "resets_at": 1788403732},
                    {"label": "7d", "used_pct": 14.0, "resets_at": 1788971433}],
        "raw": _RAW,
    }


def test_to_payload_is_plain_json_the_event_log_can_write() -> None:
    payload = _reading().to_payload()
    assert json.loads(json.dumps(payload)) == payload


def test_quota_reading_is_bookkeeping_with_no_curated_rendering() -> None:
    # It is a raw fallback line under `imp log --full` and invisible everywhere
    # else — the number is already told by the status bar and the summary.
    quota = ev("q1", "quota_reading", agent_id="step:design/spec",
               **_reading().to_payload())
    with_quota = [*SAMPLE[:-1], quota, SAMPLE[-1]]

    assert "quota_reading" not in nv.NarrativeBuilder._HANDLERS
    lean = list(render_tree(nv.build_tree(with_quota), full=False,
                            color=False, width=100))
    full = list(render_tree(nv.build_tree(with_quota), full=True,
                            color=False, width=100))
    baseline = list(render_tree(nv.build_tree(list(SAMPLE)), full=False,
                                color=False, width=100))
    assert lean == baseline                                # renders unchanged
    assert any("quota_reading" in line for line in full)   # kept under --full


def test_a_log_written_before_this_change_takes_a_reading_additively(
        tmp_path: Path) -> None:
    # A quest home recorded before this change carries no `quota_reading`, so
    # the log built over that history has no prior reading to compare against
    # and must record the first one rather than suppress it — and it appends
    # without rewriting a single existing record.
    path = tmp_path / "e.jsonl"
    path.write_text("".join(json.dumps(e) + "\n" for e in SAMPLE))
    before = list(JsonlEventLog(path).read())

    JsonlEventLog(path).append_quota_reading(agent_id="A", reading=_reading())

    after = list(JsonlEventLog(path).read())
    assert after[:len(before)] == before          # prior records untouched
    assert [e["type"] for e in after[len(before):]] == ["quota_reading"]
