"""Cycle/self-reference detection on per-step `differ_from:` edges.

Reuses a WHITE/GRAY/BLACK DFS for cycle detection over step-name edges.
Standalone module so loader.py / resolve.py / validators.py can import it
without creating an import cycle.
"""

from __future__ import annotations


def find_differ_from_cycle(edges: dict[str, str]) -> list[str] | None:
    """Return a cycle of step names in `edges` (step -> differ_from target),
    or None if acyclic. A self-reference is a length-1 cycle and is always
    reported. Each step has at most one outgoing edge."""
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

    for node in edges:
        if color.get(node, WHITE) == WHITE:
            found = visit(node, [])
            if found is not None:
                return found
    return None


def topo_order(steps: list[str], edges: dict[str, str]) -> list[str]:
    """Stable topological order over `steps` such that each `differ_from`
    target precedes its dependent. Assumes acyclic (caller checks first).
    Inputs are sorted for determinism."""
    order: list[str] = []
    seen: set[str] = set()

    def visit(s: str) -> None:
        if s in seen:
            return
        nxt = edges.get(s)
        if nxt is not None:
            visit(nxt)
        seen.add(s)
        order.append(s)

    for s in sorted(steps):
        visit(s)
    return order
