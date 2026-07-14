# tests/test_bash_tool.py
from __future__ import annotations

import json
from pathlib import Path

import anyio
import pytest

import imp.agents.tools.bash  # noqa: F401
from imp.agents.runner import QuestContext
from imp.agents.tools import get_registry
from imp.agents.tools.context import ToolContext
from imp.agents.tools.limits import ToolAborted


def _ctx(tmp_path: Path) -> ToolContext:
    ctx = ToolContext(
        project_root=tmp_path,
        events_path=tmp_path / "events.jsonl",
    )
    ctx.quest_context = QuestContext(
        id="test01", title="Test Quest", project_root=tmp_path,
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


class _RecLog:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def append(self, *, agent_id, type, payload):
        self.events.append((type, payload))
        return {"id": "e", "type": type}

    def warnings(self) -> list[dict]:
        return [p for t, p in self.events if t == "runner_warning"]


def test_bash_reports_overlay_setup_failure_distinctly(tmp_path: Path) -> None:
    from imp.engine.sandbox import (
        OVERLAY_SETUP_FAILED_CODE,
        OVERLAY_SETUP_FAILED_MARKER,
    )
    cmd = f"printf '%s\\n' {OVERLAY_SETUP_FAILED_MARKER} >&2; exit {OVERLAY_SETUP_FAILED_CODE}"
    out = json.loads(_run_bash(_ctx(tmp_path), {"command": cmd}))
    # A distinct sandbox-setup failure — NOT a plain command result with rc 122.
    assert out["status"] == "sandbox_setup_failed"
    assert "did NOT run" in out["error"]


def test_bash_exit_122_without_marker_is_a_plain_result(tmp_path: Path) -> None:
    # Detection is marker-driven, not returncode-driven: exit 122 alone is an
    # ordinary command exit, not a sandbox-setup failure.
    out = json.loads(_run_bash(_ctx(tmp_path), {"command": "exit 122"}))
    assert "status" not in out
    assert out["returncode"] == 122


def test_bash_overlay_setup_failure_emits_event(tmp_path: Path) -> None:
    from imp.engine.sandbox import (
        OVERLAY_SETUP_FAILED_CODE,
        OVERLAY_SETUP_FAILED_MARKER,
    )
    ctx = _ctx(tmp_path)
    log = _RecLog()
    ctx.event_log = log
    cmd = f"printf '%s\\n' {OVERLAY_SETUP_FAILED_MARKER} >&2; exit {OVERLAY_SETUP_FAILED_CODE}"
    json.loads(_run_bash(ctx, {"command": cmd}))
    [w] = log.warnings()
    assert w["reason"] == "overlay-setup-failed"


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


def test_bash_exposes_imp_quest_id(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    out = json.loads(_run_bash(ctx, {"command": "echo $IMP_QUEST_ID"}))
    assert out["returncode"] == 0
    assert "test01" in out["stdout"]


def test_bash_registers_and_clears_running_processes(tmp_path: Path) -> None:
    """On clean exit the process is removed from ctx.running_processes."""
    ctx = _ctx(tmp_path)
    out = json.loads(_run_bash(ctx, {"command": "true"}))
    assert out["returncode"] == 0
    assert ctx.running_processes == []


def test_bash_exposes_imp_test_cmd_fast(tmp_path: Path) -> None:
    from imp.engine.env import TestGates
    ctx = _ctx(tmp_path)
    ctx.test_gates = TestGates(fast="make check")
    out = json.loads(_run_bash(ctx, {"command": "echo $IMP_TEST_CMD_FAST"}))
    assert out["returncode"] == 0
    assert "make check" in out["stdout"]


def test_bash_no_test_gates_means_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression: a stale ambient IMP_TEST_CMD_FAST must NOT leak into the
    # subprocess when ctx has no test_gates. engine_env is authoritative;
    # overlay_engine_env strips the inherited copy.
    monkeypatch.setenv("IMP_TEST_CMD_FAST", "make stale")
    out = json.loads(_run_bash(_ctx(tmp_path),
                               {"command": "echo [$IMP_TEST_CMD_FAST]"}))
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


def test_bash_over_cap_spills_full_output_and_previews_head(tmp_path: Path) -> None:
    # Natural completion emitting > 30 KB (but < 5 MB): the FULL stream is
    # spilled to tool-results/ (recoverable), the displayed stdout is the 30 KB
    # head, a notice carries the spill path, and returncode is a normal 0.
    ctx = _ctx(tmp_path)
    out = json.loads(_run_bash(ctx, {
        "command": "yes ABCDEFGHIJ | head -c 100000"}))
    assert out["returncode"] == 0
    assert len(out["stdout"]) == 30_000              # 30 KB head display
    assert "[output truncated" not in out["stdout"]  # marker replaced by notice
    assert "tool-results" in out["notice"]
    assert "read with read_file offset=1" in out["notice"]
    assert ctx.running_processes == []
    # The spill file holds the FULL 100 KB stream, not the truncated head.
    spilled = list((tmp_path / "tool-results").iterdir())
    assert len(spilled) == 1
    data = spilled[0].read_bytes()
    assert len(data) == 100_000
    assert data.startswith(b"ABCDEFGHIJ")


def test_bash_over_cap_spill_labels_stdout_and_stderr(tmp_path: Path) -> None:
    # When stderr is non-empty, the single spill file carries BOTH tails under
    # labeled sections so one read_file recovers each.
    ctx = _ctx(tmp_path)
    out = json.loads(_run_bash(ctx, {
        "command": "yes OUT | head -c 40000; yes ERR | head -c 40000 1>&2"}))
    assert "notice" in out
    spilled = list((tmp_path / "tool-results").iterdir())
    assert len(spilled) == 1
    content = spilled[0].read_text(encoding="utf-8")
    assert "=== stdout ===" in content
    assert "=== stderr ===" in content
    assert "OUT" in content and "ERR" in content


def test_bash_caps_runaway_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # AC 5: a foreground command whose COMBINED scratch-file size crosses the cap
    # is cap-killed on the own-cap poll (both cap and cadence shrunk here) and
    # returns a NORMAL result: returncode 0, the displayed stdout truncated to the
    # 30 KB head (no inline marker), and a spill notice. The SIGKILL is cleanup,
    # not a tool_killed signal. Reading the cap through the limits module is what
    # lets this test shrink it (the old in-memory drain path ignored it).
    import imp.agents.tools.bash as bash_mod
    from imp.agents.tools import limits
    monkeypatch.setattr(limits, "BG_STREAM_FILE_CAP_BYTES", 50_000)
    monkeypatch.setattr(bash_mod, "_FG_CAP_POLL_S", 0.05)
    ctx = _ctx(tmp_path)

    async def go() -> dict:
        with anyio.fail_after(10):
            raw = await get_registry()["bash"].async_invoke(
                ctx, {"command": "while true; do echo "
                                 "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx; done"})
            return json.loads(raw)

    out = anyio.run(go)
    assert out["returncode"] == 0                    # cap is a NORMAL result
    assert len(out["stdout"]) == 30_000
    assert "[output truncated" not in out["stdout"]
    assert "notice" in out
    assert (tmp_path / "tool-results").exists()
    assert ctx.running_processes == []


def test_bash_caps_runaway_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # As above but the flood is on stderr: the combined file size still trips the
    # cap, the displayed stderr is truncated to the 30 KB head, and returncode 0.
    import imp.agents.tools.bash as bash_mod
    from imp.agents.tools import limits
    monkeypatch.setattr(limits, "BG_STREAM_FILE_CAP_BYTES", 50_000)
    monkeypatch.setattr(bash_mod, "_FG_CAP_POLL_S", 0.05)
    ctx = _ctx(tmp_path)

    async def go() -> dict:
        with anyio.fail_after(10):
            raw = await get_registry()["bash"].async_invoke(
                ctx, {"command": "while true; do echo "
                                 "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx 1>&2; done"})
            return json.loads(raw)

    out = anyio.run(go)
    assert out["returncode"] == 0
    assert len(out["stderr"]) == 30_000
    assert "[output truncated" not in out["stderr"]
    assert "notice" in out
    assert (tmp_path / "tool-results").exists()
    assert ctx.running_processes == []


def test_bash_default_foreground_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # AC 1: with NO per-call timeout, a command that outruns the DEFAULT foreground
    # deadline is PROMOTED to a background task (not SIGKILL'd / ToolAborted).
    # Monkeypatch the default tiny; assert the running-notice shape.
    import imp.agents.tools.bash as bash_mod
    from imp.agents.tools.bg import terminate_background_processes

    monkeypatch.setattr(bash_mod, "BASH_FOREGROUND_TIMEOUT_S", 0.3)
    ctx = _ctx(tmp_path)

    async def go() -> dict:
        with anyio.fail_after(15):
            out = json.loads(await get_registry()["bash"].async_invoke(
                ctx, {"command": "sleep 30"}))
            await terminate_background_processes(ctx)   # cleanup the promoted proc
            return out

    out = anyio.run(go)
    assert out["id"] == "bg1"
    assert out["status"] == "running"
    assert "bash_output" in out["note"]
    assert ctx.running_processes == []


def test_bash_normal_output_uncapped(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    out = json.loads(_run_bash(ctx, {"command": "echo hello"}))
    assert out["stdout"].strip() == "hello"
    assert "[output truncated" not in out["stdout"]
    assert "notice" not in out                       # <= 30 KB is unchanged
    assert out["returncode"] == 0


def test_bash_relative_cd_works(tmp_path: Path) -> None:
    sub = tmp_path / "src"
    sub.mkdir()
    out = json.loads(_run_bash(_ctx(tmp_path), {"command": "cd src && pwd"}))
    assert out["returncode"] == 0
    assert str(sub) in out["stdout"]


def test_bash_wraps_with_active_policy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import imp.agents.tools.bash as bashmod
    from imp.engine.sandbox import SandboxPolicy

    captured: dict[str, object] = {}

    async def fake_open_process(argv: list[str], **kw: object) -> object:
        captured["argv"] = argv
        raise RuntimeError("stop after capture")   # we only assert the argv

    monkeypatch.setattr(bashmod.anyio, "open_process", fake_open_process)
    ctx = _ctx(tmp_path)
    ctx.base_sandbox_policy = SandboxPolicy(
        active=True, worktree=str(tmp_path), git_common_dir=str(tmp_path / ".git"),
        git_ro_remask=(), writable_caches=(), extra_readable=(), extra_writable=(),
        secret_dirs=(), secret_files=(),
        setenv=(("PATH", "/usr/bin"),), network=True, note="",
    )
    # _run_bash propagates the RuntimeError out via anyio.run
    with pytest.raises(RuntimeError):
        _run_bash(ctx, {"command": "echo hi"})
    assert captured["argv"][0] == "bwrap"
    assert captured["argv"][-3:] == ["sh", "-c", "echo hi"]


def test_bash_bare_when_no_policy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import imp.agents.tools.bash as bashmod

    captured: dict[str, object] = {}

    async def fake_open_process(argv: list[str], **kw: object) -> object:
        captured["argv"] = argv
        raise RuntimeError("stop")

    monkeypatch.setattr(bashmod.anyio, "open_process", fake_open_process)
    # no sandbox_policy set on ctx — should use bare sh -c
    with pytest.raises(RuntimeError):
        _run_bash(_ctx(tmp_path), {"command": "echo hi"})
    assert captured["argv"] == ["sh", "-c", "echo hi"]


def test_bash_pending_pause_never_spawns_child(tmp_path: Path) -> None:
    # §4 pre-spawn contract: a pending pause aborts before the child spawns.
    class _PausedCoord:
        should_pause = True

    ctx = _ctx(tmp_path)
    ctx.coordinator = _PausedCoord()
    sentinel = tmp_path / "ran"
    with pytest.raises(ToolAborted) as ei:
        _run_bash(ctx, {"command": f"touch {sentinel}"})
    assert ei.value.reason == "interrupted"
    assert not sentinel.exists()          # child never spawned
    assert ctx.running_processes == []


def test_bash_editor_git_op_completes_without_blocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A hostile ambient editor that exits nonzero must NOT reach the agent's git
    # subprocess: engine_env forces GIT_EDITOR=true and overlay_engine_env strips
    # the ambient copy, so `git commit --amend` (no -m) keeps the prepared message
    # and exits 0 instead of aborting on (or blocking in) the editor.
    monkeypatch.setenv("GIT_EDITOR", "false")
    monkeypatch.setenv("EDITOR", "false")
    monkeypatch.setenv("VISUAL", "false")
    ctx = _ctx(tmp_path)
    setup = _run_bash(ctx, {"command": (
        "git init -q && git config user.email a@b.c && git config user.name t "
        "&& git commit -q --allow-empty -m seed")})
    assert json.loads(setup)["returncode"] == 0

    amended = json.loads(_run_bash(ctx, {"command": "git commit --amend --allow-empty"}))
    assert amended["returncode"] == 0

    # Observable consequence: the amend kept the single seed commit intact.
    log = json.loads(_run_bash(ctx, {"command": "git log --format=%s"}))
    assert log["stdout"].split() == ["seed"]


def test_bash_midcommand_pause_kills_and_returns_tombstone(tmp_path: Path) -> None:
    # §4 mid-command contract: a pause landing WHILE a command runs kills the
    # child's group and returns the terminated-by-pause tombstone (status:
    # killed, kill_reason: interrupted) — the same shape
    # terminate_background_processes writes — instead of blocking to the wall
    # clock. fail_after(10) makes the "poller absent" regression a fast failure
    # rather than a 30s hang.
    class _Coord:
        should_pause = False

    coord = _Coord()
    ctx = _ctx(tmp_path)
    ctx.coordinator = coord
    holder: dict[str, dict] = {}

    async def _scenario() -> None:
        with anyio.fail_after(10):
            async with anyio.create_task_group() as tg:
                async def _runner() -> None:
                    holder["out"] = json.loads(
                        await get_registry()["bash"].async_invoke(
                            ctx, {"command": "sleep 30"}))

                async def _pauser() -> None:
                    await anyio.sleep(0.3)      # let bash spawn + register first
                    coord.should_pause = True

                tg.start_soon(_runner)
                tg.start_soon(_pauser)

    anyio.run(_scenario)
    out = holder["out"]
    assert out["status"] == "killed"
    assert out["kill_reason"] == "interrupted"
    assert out["returncode"] < 0               # real signal kill, not massaged to 0
    assert ctx.running_processes == []


def test_bash_midcommand_pause_over_cap_spills_tombstone(tmp_path: Path) -> None:
    # A pause landing mid-command after the command already emitted > 30 KB must
    # spill the full output like the normal path: the tombstone keeps its
    # status/kill_reason but its displayed stdout is the 30 KB head + a notice,
    # not up to the 5 MB kill ceiling inline.
    class _Coord:
        should_pause = False

    coord = _Coord()
    ctx = _ctx(tmp_path)
    ctx.coordinator = coord
    holder: dict[str, dict] = {}

    async def _scenario() -> None:
        with anyio.fail_after(10):
            async with anyio.create_task_group() as tg:
                async def _runner() -> None:
                    holder["out"] = json.loads(
                        await get_registry()["bash"].async_invoke(
                            ctx, {"command":
                                  "yes ABCDEFGHIJ | head -c 100000; sleep 30"}))

                async def _pauser() -> None:
                    await anyio.sleep(0.5)      # let the 100 KB flush first
                    coord.should_pause = True

                tg.start_soon(_runner)
                tg.start_soon(_pauser)

    anyio.run(_scenario)
    out = holder["out"]
    assert out["status"] == "killed"
    assert out["kill_reason"] == "interrupted"
    assert len(out["stdout"]) == 30_000              # 30 KB head display
    assert "notice" in out
    spilled = list((tmp_path / "tool-results").iterdir())
    assert len(spilled) == 1
    assert len(spilled[0].read_bytes()) == 100_000   # FULL output recoverable
    assert ctx.running_processes == []


def test_bash_exposes_pythonpath_from_ctx(tmp_path: Path) -> None:
    # The bash tool prepends the worktree-resolved pythonpath roots onto the
    # subprocess PYTHONPATH so an editable install can't shadow the worktree.
    ctx = _ctx(tmp_path)
    ctx.pythonpath = ("src",)
    out = json.loads(_run_bash(ctx, {"command": "echo $PYTHONPATH"}))
    assert out["returncode"] == 0
    assert str(tmp_path / "src") in out["stdout"]


def test_bash_no_worktree_pythonpath_when_ctx_unset(tmp_path: Path) -> None:
    # Default ctx.pythonpath == (): no worktree/src prefix is injected.
    ctx = _ctx(tmp_path)
    out = json.loads(_run_bash(ctx, {"command": "echo $PYTHONPATH"}))
    assert str(tmp_path / "src") not in out["stdout"]


def test_bash_with_coordinator_no_pause_completes_normally(tmp_path: Path) -> None:
    # Regression: with a coordinator present but NO pause, the poller must stay
    # out of the way — the command completes normally and returns the ordinary
    # {stdout, stderr, returncode} result (no tombstone fields), and the call
    # returns (the poller is cancelled, not left holding the task group open).
    class _Coord:
        should_pause = False

    ctx = _ctx(tmp_path)
    ctx.coordinator = _Coord()
    out = json.loads(_run_bash(ctx, {"command": "echo hi"}))
    assert out["returncode"] == 0
    assert "hi" in out["stdout"]
    assert "status" not in out                 # normal result, not a tombstone
    assert "kill_reason" not in out
    assert ctx.running_processes == []


def test_effective_foreground_timeout_defaults_and_clamps() -> None:
    # AC 4: None/0 -> default; a value under the cap is used as-is; a value ABOVE
    # the cap clamps down to the cap. Expectations derive from the spec's 120/600.
    from imp.agents.tools.bash import _effective_foreground_timeout
    from imp.agents.tools.limits import (
        BASH_FOREGROUND_TIMEOUT_MAX_S,
        BASH_FOREGROUND_TIMEOUT_S,
    )
    assert _effective_foreground_timeout(None) == BASH_FOREGROUND_TIMEOUT_S
    assert _effective_foreground_timeout(0) == BASH_FOREGROUND_TIMEOUT_S
    assert _effective_foreground_timeout(60) == 60
    assert _effective_foreground_timeout(5000) == BASH_FOREGROUND_TIMEOUT_MAX_S


def test_bash_schema_advertises_timeout_param() -> None:
    # AC 4: the tool the model sees exposes `timeout` (optional) and its
    # description states the positive-seconds / 600 s-cap contract.
    tool = get_registry()["bash"]
    assert "timeout" in tool.input_schema["properties"]
    assert "timeout" not in tool.input_schema.get("required", [])
    assert "600" in tool.description
    assert "120" in tool.description


def test_bash_background_ignores_timeout(tmp_path: Path) -> None:
    # AC 4: run_in_background=True structurally bypasses the foreground deadline —
    # a tiny `timeout` does NOT abort the spawn; the call returns a background
    # descriptor ({id, ...}) at once and the child keeps running past that timeout.
    import contextlib as _cl
    import signal as _sig

    from imp.agents.tools.context import kill_process_group

    ctx = _ctx(tmp_path)
    ctx.agent_id = "step:t/coder"
    ctx.background_enabled = True

    async def go() -> dict:
        raw = await get_registry()["bash"].async_invoke(
            ctx, {"command": "sleep 30", "run_in_background": True, "timeout": 0.1})
        out = json.loads(raw)
        for bp in ctx.background_processes.values():
            await kill_process_group(bp.proc, _sig.SIGKILL)
            with _cl.suppress(Exception):
                await bp.proc.wait()
        return out

    out = anyio.run(go)
    assert "id" in out              # a background descriptor, not a timed-out abort
    assert "returncode" not in out  # bg spawn returns immediately, no rc yet
    assert ctx.running_processes == []


def test_bash_foreground_cap_is_30kb_not_50kb(tmp_path: Path) -> None:
    # AC 6: bash inline foreground output truncates at 30 KB (its OWN bash-specific
    # cap), NOT the 50 KB shared read_file/grep display cap; the full output still
    # spills to a recoverable file with a notice.
    from imp.agents.tools.limits import BASH_OUTPUT_CAP_BYTES
    ctx = _ctx(tmp_path)
    out = json.loads(_run_bash(ctx, {"command": "yes ABCDEFGHIJ | head -c 100000"}))
    assert BASH_OUTPUT_CAP_BYTES == 30_000
    assert len(out["stdout"]) == BASH_OUTPUT_CAP_BYTES
    assert len(out["stdout"]) != 50_000     # regression guard vs the old shared cap
    assert "notice" in out
    spilled = list((tmp_path / "tool-results").iterdir())
    assert len(spilled) == 1
    assert len(spilled[0].read_bytes()) == 100_000   # FULL output recoverable


def test_bash_foreground_stdin_is_devnull(tmp_path: Path) -> None:
    # AC 4: foreground now spawns with stdin=DEVNULL (via the shared
    # spawn-to-files core), not the old pipe path's inherited stdin. The child's
    # fd 0 points at /dev/null, so a stdin reader gets instant EOF instead of
    # hanging on a prompt or stealing the runner's stdin. readlink is the decisive
    # observable: the old inherited-stdin path never pointed fd 0 at /dev/null.
    out = json.loads(_run_bash(_ctx(tmp_path),
                               {"command": "readlink /proc/self/fd/0"}))
    assert out["returncode"] == 0
    assert out["stdout"].strip() == "/dev/null"


def test_bash_foreground_unlinks_scratch_files(tmp_path: Path) -> None:
    # A non-promoted foreground command reads its scratch files whole then unlinks
    # them: no fg-<uuid> files are left behind in the per-agent bg dir.
    from imp.agents.tools.bg import _bg_dir
    ctx = _ctx(tmp_path)
    out = json.loads(_run_bash(ctx, {"command": "echo hi"}))
    assert out["returncode"] == 0
    assert "hi" in out["stdout"]
    assert list(_bg_dir(ctx).glob("fg-*")) == []


def test_bash_foreground_timeout_promotes_to_background(tmp_path: Path) -> None:
    # AC 1 & 2: a foreground command that outruns its (per-call) deadline is NOT
    # aborted — it is promoted to a managed background task. The result carries a
    # fresh bgN id, an int pid, status "running", the canonical stream-file paths,
    # and a note naming bash_output/bash_kill; the proc lands in
    # background_processes and leaves running_processes.
    from imp.agents.tools.bg import terminate_background_processes
    ctx = _ctx(tmp_path)

    async def go() -> dict:
        with anyio.fail_after(15):
            out = json.loads(await get_registry()["bash"].async_invoke(
                ctx, {"command": "sleep 30", "timeout": 0.3}))
            await terminate_background_processes(ctx)
            return out

    out = anyio.run(go)
    assert out["id"] == "bg1"
    assert isinstance(out["pid"], int)
    assert out["status"] == "running"
    assert "bash_output" in out["note"]
    assert "bash_kill" in out["note"]
    assert out["out_path"].endswith("bg1.out")
    assert out["err_path"].endswith("bg1.err")
    assert ctx.running_processes == []
    assert "bg1" in ctx.background_processes


def test_bash_promotion_preserves_pre_promotion_output(tmp_path: Path) -> None:
    # AC 3: output produced BEFORE promotion is preserved in the renamed bgN file
    # and returned by the first bash_output("bg1") (read from cursor 0); the
    # promoted files are canonically named bg1.out/bg1.err and the fg-<uuid>
    # scratch names are renamed away (so resume seeding cannot re-alias the id).
    from imp.agents.tools.bg import _bg_dir, terminate_background_processes
    ctx = _ctx(tmp_path)

    async def go() -> tuple:
        with anyio.fail_after(15):
            promo = json.loads(await get_registry()["bash"].async_invoke(
                ctx, {"command": "echo BEFORE_PROMO; sleep 30", "timeout": 1.0}))
            snap = json.loads(await get_registry()["bash_output"].async_invoke(
                ctx, {"id": "bg1"}))
            files = sorted(p.name for p in _bg_dir(ctx).glob("bg1.*"))
            leftover_fg = list(_bg_dir(ctx).glob("fg-*"))
            await terminate_background_processes(ctx)
            return promo, snap, files, leftover_fg

    promo, snap, files, leftover_fg = anyio.run(go)
    assert promo["id"] == "bg1"
    assert "BEFORE_PROMO" in snap["stdout"]
    assert files == ["bg1.err", "bg1.out"]
    assert leftover_fg == []


def test_bash_promoted_proc_is_killable_and_torn_down(tmp_path: Path) -> None:
    # AC 2 & 9: a promoted proc is managed exactly like an explicitly-backgrounded
    # one — bash_kill("bg1") stops it (agent kill) and
    # terminate_background_processes persists a tombstone for it.
    from imp.agents.tools.bg import (
        _tombstone_path,
        terminate_background_processes,
    )
    ctx = _ctx(tmp_path)

    async def go() -> dict:
        with anyio.fail_after(15):
            json.loads(await get_registry()["bash"].async_invoke(
                ctx, {"command": "sleep 30", "timeout": 0.3}))
            killed = json.loads(await get_registry()["bash_kill"].async_invoke(
                ctx, {"id": "bg1"}))
            await terminate_background_processes(ctx)
            return killed

    killed = anyio.run(go)
    assert killed["status"] == "killed"
    assert killed["kill_reason"] == "agent"
    assert _tombstone_path(ctx).exists()
