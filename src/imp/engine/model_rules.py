"""Parse and validate cross-role model constraints (model_rules:).

Shared by providers/config.py (config-level model_rules), engine/loader.py
(workflow-level model_rules), and providers/resolve.py (cycle detection on the
merged constraint graph). Kept in its own module so none of those three need to
import each other (which would risk an import cycle).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from imp.engine._yaml import items, loc
from imp.engine.errors import LoadError
from imp.engine.types import RoleConstraint, SourceLoc


def parse_role_constraints(raw: Any, yaml_path: Path) -> dict[str, RoleConstraint]:
    """Parse a `model_rules:` mapping into {role: RoleConstraint}.

        model_rules:
          code_review:
            differ_provider_from: code

    `differ_provider_from` is the only constraint key in this design. Absent or
    null `model_rules:` parses to an empty dict.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise LoadError(
            "model_rules: must be a mapping of role to constraints",
            SourceLoc(file=yaml_path, line=1),
        )
    out: dict[str, RoleConstraint] = {}
    for role, spec in items(raw):
        rsrc = loc(yaml_path, spec) if isinstance(spec, dict) else SourceLoc(
            file=yaml_path, line=1)
        if not isinstance(spec, dict):
            raise LoadError(f"model_rules[{role!r}] must be a mapping", rsrc)
        differ = spec.get("differ_provider_from")
        if differ is not None and not isinstance(differ, str):
            raise LoadError(
                f"model_rules[{role!r}].differ_provider_from must be a role "
                f"name (string)",
                rsrc,
            )
        out[str(role)] = RoleConstraint(differ_provider_from=differ, source=rsrc)
    return out


def find_constraint_cycle(
    constraints: dict[str, RoleConstraint],
) -> list[str] | None:
    """Return a cycle (list of role names) in the role->differ-target graph, or
    None if acyclic. A self-reference (a role that differs from itself) is a
    length-1 cycle and is always reported. Each role has at most one outgoing
    edge (its differ_provider_from target).
    """
    edges: dict[str, str] = {
        role: c.differ_provider_from
        for role, c in constraints.items()
        if c.differ_provider_from is not None
    }
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {}

    def visit(node: str, stack: list[str]) -> list[str] | None:
        color[node] = GRAY
        stack.append(node)
        nxt = edges.get(node)
        if nxt is not None:
            if color.get(nxt, WHITE) == GRAY:
                i = stack.index(nxt)
                return [*stack[i:], nxt]
            if color.get(nxt, WHITE) == WHITE:
                found = visit(nxt, stack)
                if found is not None:
                    return found
        stack.pop()
        color[node] = BLACK
        return None

    for role in edges:
        if color.get(role, WHITE) == WHITE:
            found = visit(role, [])
            if found is not None:
                return found
    return None
