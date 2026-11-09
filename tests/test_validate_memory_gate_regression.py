"""The shipped validate-memory gate runs imp's interpreter, never PATH's python3."""
from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from imp.assets import bundled_workflow_dir
from imp.engine.env import engine_env
from imp.engine.gates import run_command
from imp.engine.loader import load_workflow
from imp.engine.sandbox import SandboxPolicy, sandbox_available
from tests.test_sandbox_namespace import _make_root, _policy_for

pytestmark = pytest.mark.timeout(180)

_GATE_TIMEOUT_S = 60
_STUB = "#!/bin/sh\necho \"ModuleNotFoundError: No module named 'imp.providers'\" >&2\nexit 1\n"
_BARE_PY3 = 'python3 -m imp.providers.validate_memory_cli "$IMP_PROJECT_ROOT"'

Runner = Callable[[str], dict]


def _gate_command() -> str:
    wf = load_workflow(bundled_workflow_dir("default"))
    return next(s for s in wf.flat_steps() if s.name == "validate-memory").command


def _write_memory(root: Path, rel: str) -> None:
    p = root / "imp" / "memory" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("Prefer tabs over spaces.\n", encoding="utf-8")


@pytest.fixture(params=["bare", "sandboxed"])
def gate(request: pytest.FixtureRequest,
         monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Path, Runner]]:
    sandboxed = request.param == "sandboxed"
    if sandboxed and not sandbox_available():
        pytest.skip("bwrap unavailable")
    root = _make_root()
    # Stubs live inside the worktree bind so the PATH entry resolves in the namespace.
    stubs = root / "stubs"
    stubs.mkdir()
    for name in ("python3", "python"):
        (stubs / name).write_text(_STUB)
        (stubs / name).chmod(0o755)
    monkeypatch.setenv("PATH", f"{stubs}{os.pathsep}{os.environ['PATH']}")
    # _policy_for bakes PATH from os.environ, so it must follow the setenv.
    policy: SandboxPolicy | None = _policy_for(root) if sandboxed else None
    env = engine_env(project_root=root, quest_id="q", quest_title="t")

    def run(command: str) -> dict:
        return run_command(command=command, cwd=str(root),
                           timeout_seconds=_GATE_TIMEOUT_S, env_extra=env,
                           policy=policy)

    try:
        yield root, run
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_control_hostile_python3_breaks_a_bare_python3_command(gate) -> None:
    root, run = gate
    _write_memory(root, "all.md")
    out = run(_BARE_PY3)
    assert out["exit"] == 1
    assert "ModuleNotFoundError" in out["stderr"]


def test_gate_passes_a_valid_tree(gate) -> None:
    root, run = gate
    _write_memory(root, "all.md")
    out = run(_gate_command())
    assert out["exit"] == 0, out


def test_gate_passes_an_absent_tree(gate) -> None:
    _root, run = gate
    out = run(_gate_command())
    assert out["exit"] == 0, out


def test_gate_reports_an_invalid_tree_from_the_validator(gate) -> None:
    root, run = gate
    _write_memory(root, "nosuchworkflow/x.md")
    out = run(_gate_command())
    assert out["exit"] == 3, out
    assert "nosuchworkflow" in out["stderr"]
    assert "ModuleNotFoundError" not in out["stderr"]
