# tests/test_bash_tool.py
from __future__ import annotations

import json
from pathlib import Path

import anyio
import pytest

import imp.agents.tools.bash  # noqa: F401
from imp.agents.runner import ChangeContext
from imp.agents.tools import get_registry
from imp.agents.tools.bash import _rewrite_leading_cd_project
from imp.agents.tools.context import ToolContext


def _ctx(tmp_path: Path) -> ToolContext:
    ctx = ToolContext(
        project_root=tmp_path,
        fs_permissions=(),
        events_path=tmp_path / "events.jsonl",
    )
    ctx.change_context = ChangeContext(
        slug="test-change", title="Test Change", project_root=tmp_path,
        workflow_dir=tmp_path / ".imp" / "change" / "workflow",
    )
    return ctx


def _run_bash(ctx, args):
    return anyio.run(get_registry()["bash"].async_invoke, ctx, args)


def test_bash_runs_simple_command(tmp_path: Path) -> None:
    out = json.loads(_run_bash(_ctx(tmp_path), {"command": "echo hi"}))
    assert out["returncode"] == 0
    assert "hi" in out["stdout"]


def test_bash_exposes_returncode(tmp_path: Path) -> None:
    out = json.loads(_run_bash(_ctx(tmp_path), {"command": "exit 7"}))
    assert out["returncode"] == 7


def test_bash_default_cwd_is_project_root(tmp_path: Path) -> None:
    out = json.loads(_run_bash(_ctx(tmp_path), {"command": "pwd"}))
    assert str(tmp_path) in out["stdout"]


def test_bash_explicit_cwd(tmp_path: Path) -> None:
    sub = tmp_path / "sub"
    sub.mkdir()
    out = json.loads(_run_bash(_ctx(tmp_path), {"command": "pwd", "cwd": str(sub)}))
    assert str(sub) in out["stdout"]


def test_bash_exposes_imp_project_root(tmp_path: Path) -> None:
    out = json.loads(_run_bash(_ctx(tmp_path), {"command": "echo $IMP_PROJECT_ROOT"}))
    assert out["returncode"] == 0
    assert str(tmp_path) in out["stdout"]


def test_bash_exposes_imp_change_slug(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    out = json.loads(_run_bash(ctx, {"command": "echo $IMP_CHANGE_SLUG"}))
    assert out["returncode"] == 0
    assert "test-change" in out["stdout"]


def test_bash_registers_and_clears_running_processes(tmp_path: Path) -> None:
    """On clean exit the process is removed from ctx.running_processes."""
    ctx = _ctx(tmp_path)
    out = json.loads(_run_bash(ctx, {"command": "true"}))
    assert out["returncode"] == 0
    assert ctx.running_processes == []


def test_bash_exposes_imp_lint_command(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    ctx.lint_command = "make lint"
    out = json.loads(_run_bash(ctx, {"command": "echo $IMP_LINT_COMMAND"}))
    assert out["returncode"] == 0
    assert "make lint" in out["stdout"]


def test_bash_no_lint_command_means_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression (found by imp-on-imp dogfooding): a stale IMP_LINT_COMMAND in the
    # ambient environment must NOT leak into the subprocess when the ctx has no
    # lint_command. engine_env is authoritative; overlay_engine_env strips the
    # inherited copy. Without that, `{**os.environ, ...}` would pass "make stale"
    # straight through and falsely enable the lint gate.
    monkeypatch.setenv("IMP_LINT_COMMAND", "make stale-lint")
    out = json.loads(_run_bash(_ctx(tmp_path), {"command": "echo [$IMP_LINT_COMMAND]"}))
    assert "[]" in out["stdout"]


def test_bash_terminates_on_runner_interrupt(tmp_path: Path) -> None:
    """terminate_running_processes mid-bash drains the registry and returns
    the killed processes; bash's returncode reflects the signal."""
    from imp.agents.tools.context import terminate_running_processes

    ctx = _ctx(tmp_path)

    async def _scenario():
        async with anyio.create_task_group() as tg:
            killed_holder: dict[str, list] = {}

            async def _runner():
                out = json.loads(
                    await get_registry()["bash"].async_invoke(
                        ctx, {"command": "sleep 10"},
                    ),
                )
                killed_holder["out"] = out

            async def _killer():
                # Wait briefly so bash has time to register its proc.
                await anyio.sleep(0.2)
                killed = await terminate_running_processes(ctx, timeout=0.5)
                killed_holder["killed"] = killed

            tg.start_soon(_runner)
            tg.start_soon(_killer)

        # bash returned with a signal-killed returncode (< 0).
        assert killed_holder["out"]["returncode"] < 0
        # Registry is drained.
        assert ctx.running_processes == []
        # The killer reported at least one process.
        assert len(killed_holder["killed"]) >= 1

    anyio.run(_scenario)


@pytest.mark.parametrize("cmd,expected", [
    ("cd /project && pwd",        "cd {root} && pwd"),
    ("cd /project/src && pwd",    "cd {root}/src && pwd"),
    ('cd "/project" && pwd',      'cd "{root}" && pwd'),
    ("cd '/project' && pwd",      "cd '{root}' && pwd"),
    ("cd /project; pwd",          "cd {root}; pwd"),
    ("  cd /project && pwd",      "  cd {root} && pwd"),
    # never rewritten:
    ("cd /projectfoo && pwd",     "cd /projectfoo && pwd"),
    ("cd /project_x && pwd",      "cd /project_x && pwd"),
    ("echo /project/x",           "echo /project/x"),
    ("pytest && cd /project",     "pytest && cd /project"),  # not leading
])
def test_rewrite_leading_cd_project(cmd: str, expected: str) -> None:
    root = "/real/root"
    assert _rewrite_leading_cd_project(cmd, root) == expected.format(root=root)


def test_bash_cd_project_runs_in_root(tmp_path: Path) -> None:
    out = json.loads(_run_bash(_ctx(tmp_path), {"command": "cd /project && pwd"}))
    assert out["returncode"] == 0
    assert str(tmp_path) in out["stdout"]


def test_bash_cd_project_subdir(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    out = json.loads(_run_bash(_ctx(tmp_path), {"command": "cd /project/sub && pwd"}))
    assert out["returncode"] == 0
    assert str(tmp_path / "sub") in out["stdout"]
