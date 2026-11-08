"""The `samo71` / `fego87` quota fixture: a trimmed replay of two real quests
that shared one weekly Claude Max window on 2026-09-18.

Captured with `imp events <id> --all --json --no-pager` against the maintainer's
archive and projected down to the event envelope plus only the payload fields the
summary reads — no conversation prose, so the capture is publishable as it
stands. `samo71` is its whole log. `fego87` carries the events that overlapped
samo71's run plus, per `(agent, session)`, the one earlier `query_result` and,
per agent, the last earlier `agent_message`: another quest drawing on the same
week.

Nothing in the suite reads the live archive: conftest's `_isolate_global_config`
pins `IMP_HOME` at an absent per-test directory."""
from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any

SUBJECT_ID = "samo71"
NEIGHBOUR_ID = "fego87"

_DIR = Path(__file__).parent / "fixtures" / "quota" / "samo71"


def quota_events(quest_id: str) -> list[dict[str, Any]]:
    with gzip.open(_DIR / f"{quest_id}.jsonl.gz", "rt", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]
