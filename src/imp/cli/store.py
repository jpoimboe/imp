"""Read-only introspection seam the CLI routes its reads through."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol


class Store(Protocol):
    def get_events(self, quest_id: str) -> list[dict[str, Any]] | None: ...
    def get_artifact(self, quest_id: str, kind: str) -> bytes | None: ...


class LocalStore:
    def __init__(self, control_root: Path) -> None:
        self.control_root = control_root

    def get_events(self, quest_id: str) -> list[dict[str, Any]] | None:
        from imp.cli.main import _load_events_for_id
        return _load_events_for_id(self.control_root, quest_id)

    def get_artifact(self, quest_id: str, kind: str) -> bytes | None:
        from imp.cli.main import _show_artifact
        return _show_artifact(self.control_root, quest_id, f"{kind}/{quest_id}.md")


def make_store(control_root: Path) -> Store:
    return LocalStore(control_root)
