"""Parse and resolve the selectable model-tier menu (model_tiers:).

A *tier* is just a configured role surfaced to the running parent agent as a
runtime choice. This module owns (1) the YAML parser shared by loader.py and
config.py, and (2) the config-wholesale-replace rule. Kept standalone (like
engine/model_rules.py) so loader/config/resolve/executor import it without a
cycle.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from imp.engine.errors import LoadError
from imp.engine.types import SourceLoc

if TYPE_CHECKING:
    from imp.engine.types import Workflow
    from imp.providers.config import Config


def parse_model_tiers(raw: Any, source: SourceLoc) -> tuple[str, ...]:
    """Normalize a raw `model_tiers:` value into an ordered tuple of role names.

    None or [] -> (). Rejects a non-list value, a non-string entry, or a
    duplicate entry (an ordered menu with a repeated label is ambiguous).
    """
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise LoadError(
            f"model_tiers: must be a list of role names, got {type(raw).__name__}",
            source,
        )
    out: list[str] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, str):
            raise LoadError(
                f"model_tiers entry must be a string, got {type(entry).__name__}",
                source,
            )
        if entry in seen:
            raise LoadError(f"model_tiers has duplicate entry {entry!r}", source)
        seen.add(entry)
        out.append(entry)
    return tuple(out)


def effective_model_tiers(workflow: Workflow, config: Config) -> tuple[str, ...]:
    """The menu after applying config override. A config-level `model_tiers:`
    (even an explicit empty list = off) REPLACES the workflow's list wholesale;
    an absent config-level list (None) leaves the workflow's list in force."""
    if config.model_tiers is not None:
        return config.model_tiers
    return workflow.model_tiers
