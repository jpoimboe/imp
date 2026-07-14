"""Read-only introspection seam the CLI routes its reads through."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple, Protocol


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


class HomeDigest(NamedTuple):
    quest_id: str
    quest_json: bytes
    state_json: bytes | None
    meta: dict[str, Any]
    last_iso: str
    has_user_message: bool
    prose: str
    intent_title: str | None


class Store(Protocol):
    def get_events(self, quest_id: str) -> list[dict[str, Any]] | None: ...
    def get_artifact(self, quest_id: str, kind: str) -> bytes | None: ...
    def completed_rows(self) -> list[CompletedRow]: ...
    def backlog_readiness(self, depends_on: list[str]) -> Readiness: ...
    def scan_homes(self) -> list[HomeDigest]: ...


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

    def scan_homes(self) -> list[HomeDigest]:
        import json

        from imp.cli.main import (
            _events_have_user_message,
            _last_event_iso,
            _list_quest_homes,
            _parse_jsonl,
            _quest_home,
            _resolve_intent,
        )
        from imp.cli.render_helpers import conversation_prose

        out: list[HomeDigest] = []
        for quest_id in _list_quest_homes(self.control_root):
            cdir = _quest_home(self.control_root, quest_id)
            try:
                quest_json = (cdir / "quest.json").read_bytes()
                meta = json.loads(quest_json)
            except (FileNotFoundError, json.JSONDecodeError):
                continue
            try:
                state_json: bytes | None = (cdir / "state" / "state.json").read_bytes()
            except FileNotFoundError:
                state_json = None
            events_path = cdir / "events.jsonl"
            last_iso = _last_event_iso(events_path) or ""
            has_um = _events_have_user_message(events_path)
            try:
                parsed = _parse_jsonl(events_path.read_bytes())
            except OSError:
                parsed = []
            out.append(HomeDigest(
                quest_id, quest_json, state_json, meta, last_iso, has_um,
                conversation_prose(parsed), _resolve_intent(parsed)))
        return out


def make_store(control_root: Path) -> Store:
    return LocalStore(control_root)
