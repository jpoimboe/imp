#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""Preflight: this interpreter's packages match what the project pins.

The test interpreter (`make` runs under whatever `python3` is active) is
reconciled by hand, separately from `uv.lock` — nothing ever *installs* from the
lockfile, so `make lockcheck` validates an artifact that never reaches the
environment running the tests. Two kinds of silent drift follow:

  * MISSING — a new dependency lands, `make lockcheck` stays green, and the
    interpreter is simply without the package. Surfaces as a cryptic ImportError
    deep in the suite.
  * STALE — a package is installed at a version `uv.lock` does not pin, because
    pyproject's ranges are loose (a `foo>=1.0,<2.0` happily accepts a 1.9 the
    lockfile never resolved to). Surfaces as new type/lint errors or behavior
    changes that reproduce on no other machine.

Both were live: a Fedora 3.13 -> 3.14 upgrade wiped the interpreter, and the
obvious repair (`pip install -e '.[dev]'`) reinstalled everything at *latest*,
drifting 30+ packages off the lockfile in one command.

A third class compares the declaration against the lockfile rather than the
interpreter:

  * FLOOR — a direct dependency's declared lower bound is not the version
    `uv.lock` pins, so the range claims support for a combination nothing was
    ever run on. `uv lock --upgrade` moves the lock and leaves pyproject
    untouched, so this rots by default; every direct floor must equal its pin.

This runs first in `make check` and turns all three into one clear line: which
package, what is wrong with it, and how to fix it. Stdlib only, so it still runs
in an interpreter that hasn't been reconciled yet.

Set ALLOW_DEP_DRIFT=1 to downgrade STALE to a warning (for deliberately testing
a dependency bump before running `uv lock --upgrade`). Missing packages and floor
mismatches are always fatal — the suite cannot run without a package, and a wrong
floor is a wrong claim in published metadata.
"""

from __future__ import annotations

import os
import re
import sys
import tomllib
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

# Leading distribution name of a requirement string, before any version
# specifier, extras bracket, or environment marker (e.g. "anyio>=4,<5" -> "anyio").
_NAME = re.compile(r"^[A-Za-z0-9._-]+")

# The version in a requirement's `>=` lower bound (e.g. "anyio>=4.15.1,<5.0" -> "4.15.1").
_LOWER_BOUND = re.compile(r">=\s*([^,;\s\]]+)")


def canon(name: str) -> str:
    """PEP 503 normalization. pyproject, uv.lock, and installed metadata each
    spell the same project differently (`prompt_toolkit` / `prompt-toolkit`,
    `types-PyYAML` / `types-pyyaml`), so every lookup goes through this."""
    return re.sub(r"[-_.]+", "-", name).lower()


def declared_floors(pyproject: Path) -> dict[str, str | None]:
    """Every direct dependency ([project.dependencies] + the dev extra), in declared
    order: canonical name -> its `>=` bound, None when it declares no lower bound."""
    project = tomllib.loads(pyproject.read_text())["project"]
    reqs = list(project.get("dependencies", []))
    reqs.extend(project.get("optional-dependencies", {}).get("dev", []))
    floors: dict[str, str | None] = {}
    for req in reqs:
        # Drop the environment marker first: its own `>=` (python_version >= '3.11')
        # would otherwise parse as the requirement's floor.
        spec = req.strip().split(";", 1)[0]
        m = _NAME.match(spec)
        if m is None or canon(m.group(0)) in floors:
            continue
        bound = _LOWER_BOUND.search(spec, m.end())
        floors[canon(m.group(0))] = bound.group(1) if bound else None
    return floors


def locked_versions(lock: Path) -> dict[str, str]:
    """canonical distribution name -> the exact version uv.lock pins."""
    if not lock.exists():
        return {}
    data = tomllib.loads(lock.read_text())
    return {
        canon(pkg["name"]): pkg["version"]
        for pkg in data.get("package", [])
        if "name" in pkg and "version" in pkg
    }


def _installed(name: str) -> str | None:
    try:
        return version(name)
    except PackageNotFoundError:
        return None


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    floors = declared_floors(root / "pyproject.toml")
    locked = locked_versions(root / "uv.lock")
    project = canon(tomllib.loads((root / "pyproject.toml").read_text())["project"]["name"])

    missing = [name for name in floors if _installed(name) is None]

    # Version-check every locked package that is actually installed, not just the
    # declared ones — a transitive pinned by the lockfile can drift just as far.
    # A locked package that is absent is NOT reported here: environment markers
    # legitimately exclude some (platform-specific wheels), and anything both
    # declared and absent is already covered by `missing` above.
    stale: list[tuple[str, str, str]] = []
    for cname, want in sorted(locked.items()):
        if cname == project:          # the editable self-install carries its own version
            continue
        have = _installed(cname)
        if have is not None and have != want:
            stale.append((cname, have, want))

    # A name uv.lock does not pin has nothing to compare against (fresh clone, no lockfile).
    bad_floors = [
        (name, floor, locked[name])
        for name, floor in floors.items()
        if name in locked and floor != locked[name]
    ]

    if not missing and not stale and not bad_floors:
        return 0

    if missing:
        print(
            f"dependency check failed: {len(missing)} package(s) declared in "
            "pyproject.toml are not installed in this interpreter:",
            file=sys.stderr,
        )
        for name in missing:
            print(f"    - {name}", file=sys.stderr)

    if stale:
        drift_ok = os.environ.get("ALLOW_DEP_DRIFT") == "1"
        label = "warning" if drift_ok else "dependency check failed"
        print(
            f"{label}: {len(stale)} package(s) are installed at a version "
            "uv.lock does not pin:",
            file=sys.stderr,
        )
        for name, have, want in stale:
            print(f"    - {name}: installed {have}, uv.lock pins {want}", file=sys.stderr)

    if missing or stale:
        print(f"  interpreter: {sys.executable}", file=sys.stderr)
        print("  reconcile with:  make sync-env", file=sys.stderr)

    if bad_floors:
        print(
            f"dependency check failed: {len(bad_floors)} declared floor(s) in "
            "pyproject.toml do not match uv.lock:",
            file=sys.stderr,
        )
        for name, floor, want in bad_floors:
            declares = f">={floor}" if floor is not None else "no lower bound"
            print(
                f"    - {name}: pyproject declares {declares}, uv.lock pins {want}",
                file=sys.stderr,
            )
        print(
            "  a direct dependency's lower bound must equal the version uv.lock pins",
            file=sys.stderr,
        )
        print("  update pyproject.toml, then:  make sync-env", file=sys.stderr)

    if stale and not missing and not bad_floors and os.environ.get("ALLOW_DEP_DRIFT") == "1":
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
