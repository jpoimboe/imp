"""imp's supported embedding surface: create, list and steer quests from another
process without importing imp's internals."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from imp.api_errors import (
    DaemonUnreachable,
    ImpError,
    InvalidVar,
    NotAtGate,
    QuestNotFound,
)
from imp.remote.session.attach import DaemonStartupError

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

EVENT_SCHEMA_VERSION = 1

__all__ = [
    "EVENT_SCHEMA_VERSION",
    "DaemonStartupError",
    "DaemonUnreachable",
    "ImpError",
    "InvalidVar",
    "NotAtGate",
    "QuestHandle",
    "QuestNotFound",
    "QuestSummary",
    "create_quest",
    "list_quests",
]


@dataclass(frozen=True)
class QuestHandle:
    """A quest that exists on disk, usable wherever a quest id is accepted."""

    id: str
    title: str
    control_root: Path


@dataclass(frozen=True)
class QuestSummary:
    """One quest as `list_quests` reports it, in-flight or completed."""

    id: str
    title: str
    status: str
    step: str | None


def create_quest(
    control_root: Path,
    prompt: str,
    *,
    workflow: str | None = None,
    base: str | None = None,
    title: str | None = None,
    background: bool = False,
    vars: Mapping[str, bool | int | str] | None = None,
) -> QuestHandle:
    """Mint a quest id and write its draft synchronously, in this process.
    `background=True` also starts a detached autonomous run needing no attach."""
    from imp.cli.main import BaseResolutionError
    from imp.cli.var_flags import VarFlagError
    from imp.create import create_draft_quest
    from imp.engine.errors import LoadError, LoadErrors
    from imp.engine.worktree import PreconditionError

    try:
        quest = create_draft_quest(
            control_root, prompt,
            workflow=workflow, base=base, title=title, vars=vars)
    except VarFlagError as e:
        raise InvalidVar(str(e)) from e
    except (BaseResolutionError, PreconditionError, LoadError, LoadErrors) as e:
        raise ImpError(str(e)) from e
    if background:
        from imp.remote.daemon import spawn
        try:
            spawn.start_quest(control_root, quest.id)
        except OSError as e:
            raise ImpError(
                f"quest {quest.id} was created but its run could not start: {e}") from e
    return QuestHandle(id=quest.id, title=quest.title, control_root=control_root)


def _summary(quest_id: str, status: str, step: str, title: str) -> QuestSummary:
    # Both row sources render "no step yet" as the CLI's "-" display placeholder.
    return QuestSummary(
        id=quest_id, title=title, status=status,
        step=None if step == "-" else step)


def list_quests(control_root: Path) -> list[QuestSummary]:
    """Every quest under `control_root` — live homes first, then archived ones."""
    from imp.cli.main import _row_for_id
    from imp.engine.archive import read as archive_read
    from imp.engine.quest import live_quest_ids

    # Deliberately not `_iter_quest_ids`: its `make_store` retargets to whatever
    # IMP_SESSION_SOCKET names, ignoring control_root, while rows are read locally.
    live = live_quest_ids(control_root)
    out: list[QuestSummary] = []
    for quest_id in live:
        row = _row_for_id(control_root, quest_id)
        if row is None:
            continue
        out.append(_summary(row[0], row[1], row[2], row[4]))
    seen = set(live)
    for r in archive_read.completed_rows(control_root):
        if r[0] not in seen:
            out.append(_summary(r[0], r[1], r[2], r[4]))
    return out
