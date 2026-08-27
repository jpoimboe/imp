# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Josh Poimboeuf
"""One-time warmup importing imp's whole reachable module graph at daemon
startup, so a concurrent on-disk rewrite of `src/imp/*` can't later splice
newer code into an otherwise-stale running process via a first-time import.
"""
from __future__ import annotations

import importlib
import pkgutil
import sys

WARMUP_PACKAGES = (
    "imp.engine",
    "imp.providers",
    "imp.agents",
    "imp.remote",
    "imp.cli",
)


def warm_module_graph() -> list[tuple[str, BaseException]]:
    """Import every module under the five imp subpackages, best-effort.

    A per-module import failure is caught, logged to stderr, and collected into
    the returned (name, exception) list — never raised.
    """
    failures: list[tuple[str, BaseException]] = []

    def _record(name: str, exc: BaseException) -> None:
        failures.append((name, exc))
        print(f"imp: warmup skipped {name}: {exc!r}", file=sys.stderr)

    def _on_walk_error(name: str) -> None:
        exc = sys.exc_info()[1]
        _record(name, exc if exc is not None else ImportError(name))

    for pkg_name in WARMUP_PACKAGES:
        try:
            pkg = importlib.import_module(pkg_name)
        except Exception as exc:
            _record(pkg_name, exc)
            continue
        for info in pkgutil.walk_packages(
            pkg.__path__, prefix=f"{pkg_name}.", onerror=_on_walk_error
        ):
            try:
                importlib.import_module(info.name)
            except Exception as exc:
                _record(info.name, exc)
    return failures
