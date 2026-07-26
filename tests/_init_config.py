"""Produce the real bytes `imp init` writes, for tests that must edit a genuine
init-written `imp/config.yaml`."""

from __future__ import annotations

import subprocess
from pathlib import Path

from imp.cli.init import InitSettings, _bootstrap_user_config, write_init_files


def write_real_global_config(path: Path) -> str:
    """Run `imp init`'s own global-config writer at `path` and return its text."""
    _bootstrap_user_config(path, ["anthropic"], "dark", allow_unsandboxed=False)
    return path.read_text()


def write_real_init_config(root: Path) -> str:
    """Run `imp init`'s own writer in a fresh repo at `root` — a project with a
    fast test command and no detected pythonpath, whose last live key is
    `test_cmd_fast` — and return the written file's text."""
    for cmd in (("git", "init", "-q", "-b", "main"),
                ("git", "config", "user.email", "t@t"),
                ("git", "config", "user.name", "t")):
        subprocess.run(cmd, cwd=root, check=True, capture_output=True)
    write_init_files(root, InitSettings(
        default_workflow="default", default_base="main",
        test_cmd_fast="make test", pythonpath=[]))
    return (root / "imp" / "config.yaml").read_text()
