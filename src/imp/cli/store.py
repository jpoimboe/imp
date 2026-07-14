"""Read-only introspection seam the CLI routes its reads through."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True)
class CompletedRow:
    quest_id: str
    status: str
    step: str
    last: str
    title: str
    created: str


@dataclass(frozen=True)
class Readiness:
    ready: bool
    blocked_on: tuple[str, ...]


class Store(Protocol):
    def get_events(self, quest_id: str) -> list[dict[str, Any]] | None: ...
    def get_artifact(self, quest_id: str, kind: str) -> bytes | None: ...
    def completed_rows(self) -> list[CompletedRow]: ...
    def backlog_readiness(self, depends_on: list[str]) -> Readiness: ...


class LocalStore:
    def __init__(self, control_root: Path) -> None:
        self.control_root = control_root

    def get_events(self, quest_id: str) -> list[dict[str, Any]] | None:
        from imp.cli.main import _load_events_for_id
        return _load_events_for_id(self.control_root, quest_id)

    def get_artifact(self, quest_id: str, kind: str) -> bytes | None:
        from imp.cli.main import _show_artifact
        return _show_artifact(self.control_root, quest_id, f"{kind}/{quest_id}.md")

    def completed_rows(self) -> list[CompletedRow]:
        from imp.engine.archive import read as archive_read
        return [CompletedRow(r[0], r[1], r[2], r[3], r[4], r[5])
                for r in archive_read.completed_rows(self.control_root)]

    def backlog_readiness(self, depends_on: list[str]) -> Readiness:
        from imp.engine import backlog
        ready, unmerged = backlog.readiness(self.control_root, depends_on)
        return Readiness(ready, tuple(unmerged))


def make_store(control_root: Path) -> Store:
    return LocalStore(control_root)
