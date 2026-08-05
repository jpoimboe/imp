"""The single in-process quest-creation path: `imp new`'s preflight, then a deferred
draft minted in the caller's own process."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from imp.engine.quest import Quest


def create_draft_quest(
    control_root: Path,
    prompt: str,
    *,
    workflow: str | None = None,
    base: str | None = None,
    title: str | None = None,
    vars: Mapping[str, bool | int | str] | None = None,
) -> Quest:
    """A deferred draft carrying `prompt` as its seed opening. Preflight and variable
    validation run first, so a rejection leaves nothing behind on disk."""
    from imp.cli.main import BaseResolutionError, _resolve_new_base
    from imp.cli.var_flags import validate_var_overrides
    from imp.engine import worktree
    from imp.engine.errors import LoadError
    from imp.engine.quest import Quest, live_stems
    from imp.engine.quest_id import new_quest_id
    from imp.providers.config import validate_workflow_name

    worktree.check_preconditions(control_root)
    resolved_base = _resolve_new_base(control_root, base)
    worktree.ensure_imp_ignored(control_root, resolved_base)
    if workflow is not None and not validate_workflow_name(workflow):
        raise LoadError(f"invalid workflow name {workflow!r}")
    quest_vars = validate_var_overrides(control_root, workflow=workflow, values=vars)
    try:
        return Quest.create_draft(
            control_root,
            title=title or "",
            quest_id=new_quest_id(existing_stems=live_stems(control_root)),
            base_branch=resolved_base,
            workflow=workflow,
            origin="draft",
            seed_opening=prompt,
            defer_seed=True,
            vars=quest_vars,
        )
    except RuntimeError as e:
        # The base branch can vanish between resolution and head_of.
        raise BaseResolutionError(
            f"base branch {resolved_base!r} could not be resolved: {e}") from e
