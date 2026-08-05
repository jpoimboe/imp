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
    "create_quest",
]


@dataclass(frozen=True)
class QuestHandle:
    """A quest that exists on disk, usable wherever a quest id is accepted."""

    id: str
    title: str
    control_root: Path


def create_quest(
    control_root: Path,
    prompt: str,
    *,
    workflow: str | None = None,
    base: str | None = None,
    title: str | None = None,
    vars: Mapping[str, bool | int | str] | None = None,
) -> QuestHandle:
    """Mint a quest id and write its draft synchronously, in this process."""
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
    return QuestHandle(id=quest.id, title=quest.title, control_root=control_root)
