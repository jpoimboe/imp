from __future__ import annotations

import gzip
import io
import json
import re
from pathlib import Path

from imp.cli.live_driver import LiveDriver
from imp.cli.live_status import LiveStatus

_JOURNAL = (Path(__file__).resolve().parent.parent
            / "docs" / "examples" / "seru99.jsonl.gz")
_EVENT_KEYS = {"id", "parent_id", "agent_id", "timestamp", "type", "payload", "ordinal"}


class _Pipe(io.StringIO):
    def isatty(self) -> bool:
        return False


def _events() -> list[dict]:
    with gzip.open(_JOURNAL, "rt", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh]


def test_demo_journal_is_line_delimited_json_ending_on_the_merge() -> None:
    events = _events()

    assert len(events) == 1211
    assert all(set(event) >= _EVENT_KEYS for event in events)
    assert [event["ordinal"] for event in events] == list(range(1, 1212))
    assert events[-1]["type"] == "quest_merged"


def test_demo_journal_replays_end_to_end_through_the_live_driver() -> None:
    out = _Pipe()
    driver = LiveDriver(stream=out, color=False, width=80, status=LiveStatus(out))
    for event in _events():
        driver.on_event(event)
    driver.finish()

    lines = [line for line in out.getvalue().splitlines() if line.strip()]
    assert lines[0] == "> add a --json flag to imp list"
    assert lines[-1] == "integration phase · merging · complete"


_PRIVATE = re.compile(
    r"patent|uspto|claim-scope|claim-report|disclosure-gate|spec-numeral-check", re.I)


def test_demo_journal_carries_no_private_quest_titles() -> None:
    events = _events()

    assert [e["ordinal"] for e in events if _PRIVATE.search(json.dumps(e))] == []
