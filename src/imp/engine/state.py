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
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import filelock

Status = Literal["running", "paused", "completed", "errored"]


@dataclass
class CurrentStep:
    transcript_dir: Path

    def to_dict(self) -> dict[str, Any]:
        return {"transcript_dir": str(self.transcript_dir)}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> CurrentStep:
        return cls(transcript_dir=Path(raw["transcript_dir"]))


@dataclass
class State:
    change_id: int
    phase: str | None
    step: str | None
    step_attempt: int
    phase_attempt: int
    status: Status
    current_step: CurrentStep | None = None

    @classmethod
    def load(cls, state_dir: Path) -> State:
        data = json.loads((state_dir / "state.json").read_text())
        cur = data.pop("current_step", None)
        return cls(
            **data,
            current_step=CurrentStep.from_dict(cur) if cur else None,
        )

    def save(self, state_dir: Path) -> None:
        """Atomic write: tmp file in same dir, fsync, rename."""
        state_dir.mkdir(parents=True, exist_ok=True)
        target = state_dir / "state.json"
        payload = {
            "change_id": self.change_id,
            "phase": self.phase, "step": self.step,
            "step_attempt": self.step_attempt, "phase_attempt": self.phase_attempt,
            "status": self.status,
            "current_step": self.current_step.to_dict() if self.current_step else None,
        }
        fd, tmp_name = tempfile.mkstemp(prefix=".state-", dir=state_dir)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(payload, f, indent=2, sort_keys=True)
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

    def cached_output_for(
        self, phase: str | None, step_name: str,
    ) -> dict[str, Any] | None:
        """Return the agent_output payload if the LAST step_started for
        (phase, step_name) is followed by an agent_output before any other
        step_started. Otherwise None.

        Keys on (phase, step_name) per Plan 2 review #8: a workflow can
        have the same step name in different phases (e.g., context-loader
        in load and verify); without the phase key, phase B would erroneously
        skip its invocation using phase A's cached output.
        """
        events = list(self.read())
        last_start = -1
        for i, e in enumerate(events):
            if e["type"] != "step_started":
                continue
            p = e["payload"]
            if p.get("step") == step_name and p.get("phase") == phase:
                last_start = i
        if last_start == -1:
            return None
        for i in range(last_start + 1, len(events)):
            t = events[i]["type"]
            if t == "agent_output":
                p = events[i]["payload"]
                return p if isinstance(p, dict) else None
            if t == "step_started":
                return None
        return None

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


class LockHeld(Exception):
    """Raised when a lock could not be acquired before the timeout."""

    def __init__(self, path: Path, held_by_pid: int | None) -> None:
        self.path = path
        self.held_by_pid = held_by_pid
        super().__init__(
            f"lock at {path} held by pid {held_by_pid}"
            if held_by_pid
            else f"lock at {path} is held"
        )


class ProjectLock:
    """File-backed advisory lock for single-Change-running enforcement.

    The lock file's content is the holder's PID so `imp status` can display it.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fl = filelock.FileLock(str(path) + ".filelock")

    @contextmanager
    def acquire(self, timeout: float = 0.0):  # type: ignore[no-untyped-def]
        try:
            self._fl.acquire(timeout=timeout)
        except filelock.Timeout as e:
            held_pid = self._read_pid()
            raise LockHeld(self.path, held_pid) from e
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(str(os.getpid()))
            yield self
        finally:
            try:
                self.path.unlink(missing_ok=True)
            finally:
                self._fl.release()

    def _read_pid(self) -> int | None:
        try:
            return int(self.path.read_text().strip())
        except (FileNotFoundError, ValueError):
            return None
