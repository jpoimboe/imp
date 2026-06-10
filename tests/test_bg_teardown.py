# tests/test_bg_teardown.py
from __future__ import annotations

import json
import os
from pathlib import Path

import anyio
import pytest

import imp.agents.tools.bash  # noqa: F401
from imp.agents.tools import get_registry
from tests.test_bg_spawn import _ctx, _spawn


async def _read(ctx, id: str) -> dict:
    raw = await get_registry()["bash_output"].async_invoke(ctx, {"id": id})
    return json.loads(raw)


def test_teardown_kills_marks_persists_and_deletes(tmp_path: Path) -> None:
    async def main() -> None:
        from imp.agents.tools.bg import (
            _tombstone_path,
            terminate_background_processes,
        )
        ctx = _ctx(tmp_path)
        out = await _spawn(ctx, "sleep 60")
        bp = ctx.background_processes[out["id"]]
        await terminate_background_processes(ctx)
        assert bp.status == "killed"
        assert bp.kill_reason == "interrupted"
        assert isinstance(bp.returncode, int)          # recorded, not null
        assert not bp.out_path.exists() and not bp.err_path.exists()
        ts = json.loads(_tombstone_path(ctx).read_text())
        assert ts[out["id"]]["status"] == "killed"
        assert ts[out["id"]]["kill_reason"] == "interrupted"
        assert ts[out["id"]]["returncode"] is not None
        assert len(ts[out["id"]]["command"]) <= 200
        # group actually dead
        with pytest.raises(ProcessLookupError):
            os.killpg(out["pid"], 0)
        # post-teardown, the SAME ctx still answers (in-memory tombstone)
        r = await _read(ctx, out["id"])
        assert r["status"] == "killed" and r["kill_reason"] == "interrupted"
    anyio.run(main)


def test_teardown_preserves_terminal_states(tmp_path: Path) -> None:
    """An exited entry keeps 'exited' + its REAL returncode (rewriting it would
    tell the resumed model imp killed a process that finished fine); an
    output-cap kill keeps its reason."""
    async def main() -> None:
        from imp.agents.tools.bg import (
            _sweep_bg,
            _tombstone_path,
            terminate_background_processes,
        )
        ctx = _ctx(tmp_path)
        out = await _spawn(ctx, "exit 4")
        bp = ctx.background_processes[out["id"]]
        with anyio.fail_after(10):
            await bp.proc.wait()
        await _sweep_bg(ctx)
        assert bp.status == "exited"
        await terminate_background_processes(ctx)
        ts = json.loads(_tombstone_path(ctx).read_text())
        assert ts[out["id"]] == {"status": "exited", "kill_reason": None,
                                 "returncode": 4, "command": "exit 4"}
    anyio.run(main)


def test_teardown_sigkills_term_immune_children_of_exited_leader(
    tmp_path: Path,
) -> None:
    """The SIGKILL pass is UNCONDITIONAL per group: an exited leader whose
    TERM-trapping child survives the SIGTERM+grace must still be dead after
    teardown — the leader-returncode proxy cannot identify survivors."""
    async def main() -> None:
        from imp.agents.tools.bg import _sweep_bg, terminate_background_processes
        ctx = _ctx(tmp_path)
        out = await _spawn(ctx, "( trap '' TERM; sleep 60 ) & exit 0")
        bp = ctx.background_processes[out["id"]]
        with anyio.fail_after(10):
            await bp.proc.wait()
        await _sweep_bg(ctx)
        assert bp.status == "exited"           # leader gone, child lives
        await terminate_background_processes(ctx)
        # Retry briefly: the orphaned child is reparented to init and reaped
        # asynchronously; a zombie still answers killpg(pgid, 0).
        with anyio.fail_after(5):
            while True:
                try:
                    os.killpg(out["pid"], 0)
                except ProcessLookupError:
                    break
                await anyio.sleep(0.05)
        # terminal state preserved: still 'exited' with the real returncode
        assert bp.status == "exited" and bp.returncode == 0
    anyio.run(main)


def test_teardown_survives_cancellation_mid_grace(tmp_path: Path) -> None:
    """Teardown is cancellation-SHIELDED: a CancelledError landing mid-grace
    (a BaseException — it escapes suppress(Exception)) must not skip the
    SIGKILL pass or the tombstone write, the two steps Decision 7 exists for.

    ADAPTATION (sanctioned): the original command "trap '' TERM; sleep 60"
    has the same trap-install race found in test_bash_kill_flips_before_signaling
    (commit 753d796): SIGTERM racing `trap` install kills the group instantly
    and teardown returns fast — the move_on_after(0.5) never fires mid-grace,
    making the shield pin hollow. We apply the same settle-loop fix: spawn
    "trap '' TERM; echo ready; sleep 60" and wait for "ready" via accumulated
    bash_output reads before calling teardown."""
    async def main() -> None:
        from imp.agents.tools.bg import (
            _tombstone_path,
            terminate_background_processes,
        )
        ctx = _ctx(tmp_path)
        out = await _spawn(ctx, "trap '' TERM; echo ready; sleep 60")   # rides out the grace

        # Wait for trap to be installed before triggering teardown:
        # same settle-loop pattern as test_bash_kill_flips_before_signaling.
        seen_out = ""
        with anyio.fail_after(10):
            while "ready" not in seen_out:
                seen_out += (await _read(ctx, out["id"]))["stdout"]
                await anyio.sleep(0.05)

        # The deadline (0.5 s) expires while teardown sits in its 4 s grace
        # window; the shield must let teardown run to completion anyway.
        with anyio.move_on_after(0.5):
            await terminate_background_processes(ctx)
        ts = json.loads(_tombstone_path(ctx).read_text())
        assert ts[out["id"]]["status"] == "killed"
        assert ts[out["id"]]["kill_reason"] == "interrupted"
        with anyio.fail_after(5):
            while True:
                try:
                    os.killpg(out["pid"], 0)
                except ProcessLookupError:
                    break
                await anyio.sleep(0.05)
    anyio.run(main)


def test_double_teardown_is_clean_noop(tmp_path: Path) -> None:
    """On a pause BOTH the interrupt path and the run() finally fire."""
    async def main() -> None:
        from imp.agents.tools.bg import terminate_background_processes
        ctx = _ctx(tmp_path)
        out = await _spawn(ctx, "sleep 60")
        await terminate_background_processes(ctx)
        first = dict(vars(ctx.background_processes[out["id"]]))
        await terminate_background_processes(ctx)        # must not raise
        second = dict(vars(ctx.background_processes[out["id"]]))
        del first["proc"], second["proc"]
        assert first == second                            # state unchanged
    anyio.run(main)


def test_resume_reads_tombstone_and_seeds_counter(tmp_path: Path) -> None:
    """The actual user-visible scenario: pause-teardown, then a FRESH
    ToolContext (same agent_id — a resume re-drives with a fresh runner).
    bash_output('bg1') answers from the persisted file; the first post-resume
    spawn is bg2, never a re-aliased bg1."""
    async def main() -> None:
        from imp.agents.tools.bg import terminate_background_processes
        ctx1 = _ctx(tmp_path)
        out = await _spawn(ctx1, "sleep 60")
        assert out["id"] == "bg1"
        await terminate_background_processes(ctx1)

        ctx2 = _ctx(tmp_path)                       # fresh context, same agent_id
        r = await _read(ctx2, "bg1")
        assert r["status"] == "killed" and r["kill_reason"] == "interrupted"
        out2 = await _spawn(ctx2, "sleep 60")
        assert out2["id"] == "bg2"
        await terminate_background_processes(ctx2)
        # SECOND pause: read-merge-write — both attempts' tombstones survive
        from imp.agents.tools.bg import _tombstone_path
        ts = json.loads(_tombstone_path(ctx2).read_text())
        assert set(ts) == {"bg1", "bg2"}
    anyio.run(main)


def test_unknown_id_after_ungraceful_death_named_residual(tmp_path: Path) -> None:
    """No teardown ran (stream files, no tombstone file): bash_output raises
    unknown-id (accepted residual), but counter seeding still prevents
    ALIASING — the next spawn skips past the filename-derived ids."""
    async def main() -> None:
        from imp.agents.tools.bg import _bg_dir
        ctx1 = _ctx(tmp_path)
        d = _bg_dir(ctx1)
        d.mkdir(parents=True)
        (d / "bg1.out").write_bytes(b"stale")
        (d / "bg1.err").write_bytes(b"")
        with pytest.raises(ValueError):
            await _read(ctx1, "bg1")
        out = await _spawn(ctx1, "true")
        assert out["id"] == "bg2"
        bp = ctx1.background_processes["bg2"]
        with anyio.fail_after(10):
            await bp.proc.wait()
    anyio.run(main)


def test_cross_invocation_namespace_isolation(tmp_path: Path) -> None:
    """Parent and subagent share the run dir but never a scratch path: each
    gets bg1 in its OWN dir; one's teardown must not delete the other's file."""
    async def main() -> None:
        from imp.agents.tools.bg import terminate_background_processes
        parent = _ctx(tmp_path, agent_id="step:plan/coder")
        sub = _ctx(tmp_path, agent_id="researcher-1")
        p_out = await _spawn(parent, "sleep 60")
        s_out = await _spawn(sub, "sleep 60")
        assert p_out["id"] == s_out["id"] == "bg1"
        assert p_out["out_path"] != s_out["out_path"]
        await terminate_background_processes(parent)
        assert (tmp_path / s_out["out_path"]).exists()   # sibling untouched
        r = await _read(sub, "bg1")
        assert r["status"] == "running"
        await terminate_background_processes(sub)
    anyio.run(main)


def test_tombstone_write_is_atomic_no_temp_left(tmp_path: Path) -> None:
    async def main() -> None:
        from imp.agents.tools.bg import _bg_dir, terminate_background_processes
        ctx = _ctx(tmp_path)
        await _spawn(ctx, "sleep 60")
        await terminate_background_processes(ctx)
        names = {p.name for p in _bg_dir(ctx).iterdir()}
        assert names == {"tombstones.json"}              # streams gone, no .tmp
    anyio.run(main)
