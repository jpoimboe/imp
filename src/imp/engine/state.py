"""Per-Change persistence: state.json (cursor) + events.jsonl (audit log) + locks.

Tasks 10/11/12 build this file incrementally:
  Task 10 — State
  Task 11 — EventLog
  Task 12 — ProjectLock + LockHeld
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

Status = Literal["running", "paused", "completed", "errored"]


@dataclass
class State:
    change_id: int
    phase: str | None
    step: str | None
    step_attempt: int
    phase_attempt: int
    status: Status

    @classmethod
    def load(cls, state_dir: Path) -> State:
        data = json.loads((state_dir / "state.json").read_text())
        return cls(**data)

    def save(self, state_dir: Path) -> None:
        """Atomic write: tmp file in same dir, fsync, rename."""
        state_dir.mkdir(parents=True, exist_ok=True)
        target = state_dir / "state.json"
        fd, tmp_name = tempfile.mkstemp(prefix=".state-", dir=state_dir)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(asdict(self), f, indent=2, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, target)
        except Exception:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp_name)
            raise


class EventLog:
    """Append-only event log for audit trail."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Append an event to the log with a timestamp."""
        record = {
            "timestamp": datetime.now(UTC).isoformat(),
            "type": event_type,
            "payload": payload,
        }
        with self.path.open("a") as f:
            f.write(json.dumps(record))
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        return record

    def read(self) -> Iterator[dict[str, Any]]:
        """Read all events from the log."""
        if not self.path.exists():
            return
        with self.path.open() as f:
            for line in f:
                line = line.strip()
                if line:
                    yield json.loads(line)

    def events_for_current_step(self, step_name: str) -> Iterable[dict[str, Any]]:
        """Replay-helper for `imp resume`: yield events from the last
        `step_started` for `step_name` until the next different step starts.
        Used to show context when resuming.
        """
        events = list(self.read())
        last_start = -1
        for i, e in enumerate(events):
            if (
                e["type"] == "step_started"
                and e["payload"].get("step") == step_name
            ):
                last_start = i
        if last_start == -1:
            return []

        # Find the end: either end of file or next step_started with different step
        end = len(events)
        for i in range(last_start + 1, len(events)):
            if (
                events[i]["type"] == "step_started"
                and events[i]["payload"].get("step") != step_name
            ):
                end = i
                break

        return events[last_start:end]
