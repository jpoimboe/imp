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
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

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
