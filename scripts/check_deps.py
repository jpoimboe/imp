#!/usr/bin/env python3
"""Preflight: every dependency pyproject.toml declares is installed *here*.

The test interpreter (`make` runs under whatever `python3` is active) is
reconciled to pyproject by hand via `pip install -e '.[dev]'`, separately from
`uv.lock`. So when a new dependency lands, `make lockcheck` can stay green while
the interpreter that actually runs the tests is silently missing the package —
the failure then surfaces as a cryptic ImportError deep in the suite.

This runs first in `make check` and turns that silent drift into one clear line:
which package is missing, in which interpreter, and how to fix it. Stdlib only,
so it still runs in an interpreter that hasn't been reconciled yet.
"""

from __future__ import annotations

import re
import sys
import tomllib
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

# Leading distribution name of a requirement string, before any version
# specifier, extras bracket, or environment marker (e.g. "anyio>=4,<5" -> "anyio").
_NAME = re.compile(r"^[A-Za-z0-9._-]+")


def declared_dependencies(pyproject: Path) -> list[str]:
    """Distribution names from [project.dependencies] + the dev extra, in order."""
    project = tomllib.loads(pyproject.read_text())["project"]
    reqs = list(project.get("dependencies", []))
    reqs.extend(project.get("optional-dependencies", {}).get("dev", []))
    names: list[str] = []
    seen: set[str] = set()
    for req in reqs:
        m = _NAME.match(req.strip())
        if m and m.group(0) not in seen:
            seen.add(m.group(0))
            names.append(m.group(0))
    return names


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    missing = [
        name
        for name in declared_dependencies(root / "pyproject.toml")
        if not _installed(name)
    ]
    if not missing:
        return 0
    print(
        f"dependency check failed: {len(missing)} package(s) declared in "
        "pyproject.toml are not installed in this interpreter:",
        file=sys.stderr,
    )
    for name in missing:
        print(f"    - {name}", file=sys.stderr)
    print(f"  interpreter: {sys.executable}", file=sys.stderr)
    print("  reconcile with:  pip install -e '.[dev]'", file=sys.stderr)
    return 1


def _installed(name: str) -> bool:
    try:
        version(name)
        return True
    except PackageNotFoundError:
        return False


if __name__ == "__main__":
    sys.exit(main())
