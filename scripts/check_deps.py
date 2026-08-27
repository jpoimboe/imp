#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
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
    pyproject's ranges are loose (`prompt_toolkit>=3.0,<4.0` happily accepts a
    3.0.53 the lockfile never resolved to). Surfaces as new type/lint errors or
    behavior changes that reproduce on no other machine.

Both were live: a Fedora 3.13 -> 3.14 upgrade wiped the interpreter, and the
obvious repair (`pip install -e '.[dev]'`) reinstalled everything at *latest*,
drifting 30+ packages off the lockfile in one command.

This runs first in `make check` and turns both into one clear line: which
package, which interpreter, and how to fix it. Stdlib only, so it still runs in
an interpreter that hasn't been reconciled yet.

Set ALLOW_DEP_DRIFT=1 to downgrade STALE to a warning (for deliberately testing
a dependency bump before running `uv lock --upgrade`). Missing packages are
always fatal — the suite cannot run without them.
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


def canon(name: str) -> str:
    """PEP 503 normalization. pyproject, uv.lock, and installed metadata each
    spell the same project differently (`prompt_toolkit` / `prompt-toolkit`,
    `types-PyYAML` / `types-pyyaml`), so every lookup goes through this."""
    return re.sub(r"[-_.]+", "-", name).lower()


def declared_dependencies(pyproject: Path) -> list[str]:
    """Distribution names from [project.dependencies] + the dev extra, in order."""
    project = tomllib.loads(pyproject.read_text())["project"]
    reqs = list(project.get("dependencies", []))
    reqs.extend(project.get("optional-dependencies", {}).get("dev", []))
    names: list[str] = []
    seen: set[str] = set()
    for req in reqs:
        m = _NAME.match(req.strip())
        if m and canon(m.group(0)) not in seen:
            seen.add(canon(m.group(0)))
            names.append(m.group(0))
    return names


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
    declared = declared_dependencies(root / "pyproject.toml")
    locked = locked_versions(root / "uv.lock")
    project = canon(tomllib.loads((root / "pyproject.toml").read_text())["project"]["name"])

    missing = [name for name in declared if _installed(name) is None]

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

    if not missing and not stale:
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

    print(f"  interpreter: {sys.executable}", file=sys.stderr)
    print("  reconcile with:  make sync-env", file=sys.stderr)
    if stale and not missing and os.environ.get("ALLOW_DEP_DRIFT") == "1":
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
