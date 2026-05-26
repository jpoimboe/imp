"""FS permission check (spec §6.7).

Called by FS tools (read_file/write_file/edit_file/glob/grep/ls).
Default-allow on read, default-deny on write — V1 inverts V0's read-default to
close the silent-permissive-write footgun.

Patterns:
  **  matches any number of segments (incl. zero)
  *   matches within a single segment (no slashes)
  /   literal
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from functools import lru_cache
from typing import Literal

from imp.engine.types import FsRule

Op = Literal["read", "write"]


class PermissionDenied(Exception):
    pass


@lru_cache(maxsize=512)
def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    parts: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern[i] == "*" and i + 1 < len(pattern) and pattern[i + 1] == "*":
            parts.append(".*")
            i += 2
        elif pattern[i] == "*":
            parts.append("[^/]*")
            i += 1
        else:
            parts.append(re.escape(pattern[i]))
            i += 1
    return re.compile(r"\A" + "".join(parts) + r"\Z")


def check_permission(rules: Sequence[FsRule], op: Op, path: str) -> None:
    for rule in rules:
        if op not in rule.operations:
            continue
        for pat in rule.paths:
            if _glob_to_regex(pat).match(path):
                if rule.mode == "deny":
                    raise PermissionDenied(
                        f"{op} on {path!r} denied by rule {pat!r} at {rule.source}"
                    )
                return
    # Defaults
    if op == "read":
        return
    raise PermissionDenied(
        f"write on {path!r} denied by default (no matching allow rule)"
    )
