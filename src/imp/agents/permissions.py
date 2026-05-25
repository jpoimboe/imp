"""Filesystem permission enforcement (spec §6.4).

The engine declares rules per-step in workflow.yaml; this module evaluates
a single (operation, path) against the rule list and either passes silently
or raises PermissionDenied.

Pattern semantics:
  **  matches any number of path segments (including zero)
  *   matches within a single segment (no slashes)
  /   literal

Default-allow when no rule matches. Workflows are expected to include a
catch-all `deny /**` rule when they want a restrictive posture.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Literal

from imp.engine.types import FsRule


class PermissionDenied(Exception):
    pass


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    parts: list[str] = []
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if c == "*" and i + 1 < len(pattern) and pattern[i + 1] == "*":
            parts.append(".*")
            i += 2
        elif c == "*":
            parts.append("[^/]*")
            i += 1
        else:
            parts.append(re.escape(c))
            i += 1
    return re.compile(r"^" + "".join(parts) + r"$")


def check_permission(
    rules: Sequence[FsRule],
    operation: Literal["read", "write"],
    path: str,
) -> None:
    for rule in rules:
        if operation not in rule.operations:
            continue
        for pat in rule.paths:
            if _glob_to_regex(pat).match(path):
                if rule.mode == "deny":
                    raise PermissionDenied(
                        f"{operation} on {path} denied by rule "
                        f"{pat!r} at {rule.source}"
                    )
                return  # allow → pass
