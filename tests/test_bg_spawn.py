# tests/test_bg_spawn.py
from __future__ import annotations

import contextlib
import json
import os
import sys
from pathlib import Path

import anyio
import pytest

import imp.agents.tools.bash  # noqa: F401  (registers bash + bg builtins)
from imp.agents.runner import ChangeContext
from imp.agents.tools import get_registry
from imp.agents.tools.context import ToolContext


def _ctx(tmp_path: Path, agent_id: str = "step:t/coder") -> ToolContext:
    run_dir = tmp_path / ".imp" / "change"
    run_dir.mkdir(parents=True, exist_ok=True)
    ctx = ToolContext(project_root=tmp_path, fs_permissions=(),
                      events_path=run_dir / "events.jsonl", agent_id=agent_id)
    ctx.change_context = ChangeContext(
        slug="t", title="T", project_root=tmp_path,
        workflow_dir=tmp_path / ".imp" / "change" / "workflow")
    return ctx


async def _spawn(ctx: ToolContext, command: str) -> dict:
    raw = await get_registry()["bash"].async_invoke(
        ctx, {"command": command, "run_in_background": True})
    return json.loads(raw)


async def _cleanup(ctx: ToolContext) -> None:
    import signal as _sig

    from imp.agents.tools.context import kill_process_group
    for bp in ctx.background_processes.values():
        await kill_process_group(bp.proc, _sig.SIGKILL)
        with contextlib.suppress(Exception):
            await bp.proc.wait()


def _fds_open_to(path: Path) -> int:
    n = 0
    for fd in os.listdir("/proc/self/fd"):
        with contextlib.suppress(OSError):
            if os.readlink(f"/proc/self/fd/{fd}") == str(path):
                n += 1
    return n


@pytest.mark.skipif(not sys.platform.startswith("linux"),
                    reason="/proc/self/fd is Linux-only")
def test_spawn_returns_immediately_registers_and_keeps_running(tmp_path: Path) -> None:
    async def main() -> None:
        ctx = _ctx(tmp_path)
        t0 = anyio.current_time()
        out = await _spawn(ctx, "sleep 30")
        assert anyio.current_time() - t0 < 5.0          # returned immediately
        assert out["id"] == "bg1"
        assert out["status"] == "running"
        assert isinstance(out["pid"], int)
        assert out["command"] == "sleep 30"
        # paths are worktree-root-relative, never absolute
        assert not Path(out["out_path"]).is_absolute()
        assert (tmp_path / out["out_path"]).exists()
        assert (tmp_path / out["err_path"]).exists()
        bp = ctx.background_processes["bg1"]
        assert bp.proc.returncode is None               # still running
        # registered ONLY on background_processes — never running_processes
        assert ctx.running_processes == []
        # parent-side fd copies are closed after spawn (no fd leak)
        assert _fds_open_to(bp.out_path) == 0
        assert _fds_open_to(bp.err_path) == 0
        await _cleanup(ctx)
    anyio.run(main)


def test_spawn_ids_increment_and_files_truncate(tmp_path: Path) -> None:
    async def main() -> None:
        ctx = _ctx(tmp_path)
        a = await _spawn(ctx, "sleep 30")
        b = await _spawn(ctx, "sleep 30")
        assert (a["id"], b["id"]) == ("bg1", "bg2")
        assert a["out_path"] != b["out_path"]
        await _cleanup(ctx)
    anyio.run(main)


def test_stdin_is_devnull_so_reader_sees_eof(tmp_path: Path) -> None:
    """A stdin-reading child must see immediate EOF, never hang or steal tty."""
    async def main() -> None:
        ctx = _ctx(tmp_path)
        out = await _spawn(ctx, 'read line; echo "got:[$line]"')
        bp = ctx.background_processes[out["id"]]
        with anyio.fail_after(10):
            await bp.proc.wait()
        assert (tmp_path / out["out_path"]).read_text() == "got:[]\n"
    anyio.run(main)


@pytest.mark.skipif(os.geteuid() == 0, reason="chmod fault injection needs non-root")
def test_pre_spawn_open_failure_registers_nothing(tmp_path: Path) -> None:
    """Forced post-dir-creation open failure aborts BEFORE the spawn: no child,
    nothing registered. The fault must NOT be a bgN.out/bgN.err-shaped
    obstruction — counter seeding parses those filenames and would skip past
    it, defeating the injection. An unwritable dir hits the bg1.out open."""
    async def main() -> None:
        from imp.agents.tools.bg import _bg_dir
        ctx = _ctx(tmp_path)
        d = _bg_dir(ctx)
        d.mkdir(parents=True)
        os.chmod(d, 0o555)             # open(bg1.out, "wb") -> PermissionError
        try:
            with pytest.raises(OSError):
                await _spawn(ctx, "echo hi")
        finally:
            os.chmod(d, 0o755)
        assert ctx.background_processes == {}
        assert list(d.iterdir()) == []        # nothing left behind
    anyio.run(main)


def test_err_open_failure_cleans_out_file(tmp_path: Path, monkeypatch) -> None:
    """The err-file open branch: out_f is closed and bg1.out unlinked."""
    async def main() -> None:
        import builtins

        import imp.agents.tools.bg as bg_mod
        from imp.agents.tools.bg import _bg_dir
        ctx = _ctx(tmp_path)
        real_open = builtins.open

        def failing_open(path, *a, **kw):
            if str(path).endswith(".err"):
                raise OSError("injected")
            return real_open(path, *a, **kw)

        # Shadow the builtin in bg's module namespace; monkeypatch removes the
        # shadow (and restores builtin resolution) on test exit.
        monkeypatch.setattr(bg_mod, "open", failing_open, raising=False)
        with pytest.raises(OSError):
            await _spawn(ctx, "echo hi")
        assert ctx.background_processes == {}
        assert not (_bg_dir(ctx) / "bg1.out").exists()   # best-effort cleanup
    anyio.run(main)


def test_foreground_bash_unchanged_and_ensure_ascii_false(tmp_path: Path) -> None:
    async def main() -> None:
        ctx = _ctx(tmp_path)
        raw = await get_registry()["bash"].async_invoke(ctx, {"command": "echo 中文"})
        assert "中文" in raw                 # no \uXXXX inflation
        assert "\\u4e2d" not in raw
        out = json.loads(raw)
        assert out["returncode"] == 0
    anyio.run(main)


def test_bash_description_rewritten() -> None:
    desc = get_registry()["bash"].description
    assert "run_in_background" in desc
    assert "bash_output" in desc and "bash_kill" in desc
    assert "does NOT validate" in desc or "not validate" in desc
    assert "block-buffer" in desc
