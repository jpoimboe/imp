from __future__ import annotations

import json

from imp.providers.quota import QuotaReading, QuotaWindow

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
