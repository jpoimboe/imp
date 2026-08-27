# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Josh Poimboeuf
"""Thin CLI entrypoint: validate a project's `imp/memory/` tree off a worktree root.

Invoked by the bundled `memory` workflow's validation gate. Loads the memory
snapshot from the given root and runs the same `validate_project_memory` the
executor runs at run entry, so "valid at the gate" == "loads at the next run".
Exits non-zero, printing the errors, on an invalid tree. imp's own tooling —
exempt from the "intent, not a runner" prompt invariant.
"""
from __future__ import annotations

import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    root = Path(args[0]) if args else Path.cwd()

    from imp.engine.errors import LoadError, LoadErrors
    from imp.providers.effective import validate_project_memory
    from imp.providers.memory import load_memory_snapshot

    try:
        memory = load_memory_snapshot(root / "imp" / "memory")
        validate_project_memory(root, memory)
    except (LoadError, LoadErrors) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
