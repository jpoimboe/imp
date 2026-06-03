"""Helpers for parsing IMP_LIVE_PROVIDERS in live-API tests.

Live tests opt in via the IMP_LIVE_PROVIDERS env var (comma-separated).
Each test's skipif checks `opted_in(<provider>)`. Auth presence is then
asserted (not skipped) — opt-in implies the user has set up credentials.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path


def _parse() -> list[str]:
    raw = os.environ.get("IMP_LIVE_PROVIDERS", "")
    return [tok.strip() for tok in raw.split(",") if tok.strip()]


def opted_in(provider: str) -> bool:
    return provider in _parse()


def first_opted_in() -> str | None:
    items = _parse()
    return items[0] if items else None


def build_isolated_venv(
    project_root: Path,
    *,
    python: str | None = None,
    extras: tuple[str, ...] = (),
) -> Path:
    """Build an ISOLATED venv (NO --system-site-packages) under
    project_root/.venv and install project_root editable + its deps into it.

    Prefers `uv` for speed; falls back to stdlib venv + pip. No
    --system-site-packages means the host's editable `imp` cannot shadow the
    copy: the venv sees only what is installed here.

    `extras` names optional-dependency groups (e.g. ("dev",)) to install with
    the editable target — the isolated venv has no host site-packages to borrow
    from, so test tools like pytest must be installed here explicitly. Empty by
    default so the stub-isolation test keeps a deliberately bare venv.

    Returns the venv's python interpreter path.
    """
    import shutil

    venv_dir = project_root / ".venv"
    base_python = python or sys.executable
    target = (
        f"{project_root}[{','.join(extras)}]" if extras else str(project_root)
    )

    if shutil.which("uv"):
        subprocess.run(
            ["uv", "venv", "--python", base_python, str(venv_dir)],
            check=True,
        )
        venv_python = venv_dir / "bin" / "python"
        subprocess.run(
            ["uv", "pip", "install", "--python", str(venv_python),
             "-e", target],
            check=True,
        )
    else:
        subprocess.run([base_python, "-m", "venv", str(venv_dir)], check=True)
        venv_python = venv_dir / "bin" / "python"
        subprocess.run(
            [str(venv_python), "-m", "pip", "install",
             "-e", target, "-q"],
            check=True,
        )
    return venv_python


def _await_worktree(control_root: Path, timeout: float) -> Path:
    """Poll .imp/worktrees/ for the single Change worktree dir and return it;
    its .name is the slug. The real guard is the presence of
    .imp/change/change.json in the candidate (scratch/.merge-* dirs lack it);
    the leading-dot exclusion is redundant belt-and-suspenders for readability.

    Replaces stdout-parsing of `imp new`'s "Created Change <slug>:" line:
    sidesteps PTY/PIPE read-timing and yields the slug directly. Raises
    TimeoutError if no Change worktree appears within `timeout` seconds."""
    root = control_root / ".imp" / "worktrees"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if root.is_dir():
            for p in sorted(root.iterdir()):
                if (p.is_dir() and not p.name.startswith(".")
                        and (p / ".imp" / "change" / "change.json").exists()):
                    return p
        time.sleep(0.1)
    raise TimeoutError(
        f"no Change worktree appeared under {root} within {timeout}s"
    )
