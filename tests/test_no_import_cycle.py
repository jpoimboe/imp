from __future__ import annotations

import subprocess
import sys

import pytest


@pytest.mark.parametrize("module", [
    "imp.engine.agent_run",
    "imp.agents.subagents",
])
def test_module_imports_cold(module: str) -> None:
    """A clean interpreter must import these without a circular-import crash.
    Guards the lazy-import of error_policy/schema in drive_invocation: a
    top-level import there would loop agent_run -> error_policy ->
    providers/__init__ -> anthropic -> agents.subagents (partial)."""
    proc = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        capture_output=True, text=True)
    assert proc.returncode == 0, (
        f"cold `import {module}` failed:\n{proc.stderr}")
