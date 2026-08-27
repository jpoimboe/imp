# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Josh Poimboeuf
"""Per-run `imp` shim so every agent bash command and script gate can invoke the
outer (running) imp as `imp`, regardless of the ambient PATH."""

from __future__ import annotations

import sys
from pathlib import Path

import imp

_SHIM_DIRNAME = "imp-shim"


def imp_shim_dir(quest_dir: Path) -> Path:
    """The per-run directory holding only the `imp` shim."""
    return quest_dir / _SHIM_DIRNAME


def write_imp_shim(quest_dir: Path) -> Path:
    """Write the console-script `imp` shim under quest_dir/imp-shim/; return the dir."""
    d = imp_shim_dir(quest_dir)
    d.mkdir(parents=True, exist_ok=True)
    outer_root = str(Path(imp.__file__).resolve().parent.parent)
    shim = d / "imp"
    # File form + sys.path[0]=outer_root: resolves to the outer imp under a PYTHONPATH
    # shadow; never `-m imp` (cwd would shadow the package).
    shim.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        f"sys.path.insert(0, {outer_root!r})\n"
        "from imp.cli.main import main\n"
        "sys.exit(main())\n"
    )
    shim.chmod(0o755)
    return d


def outer_imp_mismatch(control_root: Path) -> str | None:
    """Message when the running imp is not `control_root`'s own src; None otherwise."""
    expected_src = (control_root / "src").resolve()
    if not (expected_src / "imp" / "__init__.py").exists():
        return None
    running_src = Path(imp.__file__).resolve().parent.parent
    if running_src == expected_src:
        return None
    return (
        f"outer imp mismatch: imp is running from {running_src}, not this checkout's "
        f"{expected_src} — changes merged here take effect only after "
        f"`python3 -m pip install -e {control_root}`"
    )
