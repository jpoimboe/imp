# tests/test_bash_tool.py
from __future__ import annotations

import json
import os
import shlex
from pathlib import Path

import anyio
import pytest

import imp.agents.tools.bash  # noqa: F401
from imp.agents.runner import QuestContext
from imp.agents.tools import get_registry
from imp.agents.tools.context import ToolContext
from imp.agents.tools.limits import MATCH_LINE_CAP_BYTES, ToolAborted
from imp.engine.sandbox import SandboxSpawn
from imp.paths import project_state_dir


def _ctx(tmp_path: Path) -> ToolContext:
    ctx = ToolContext(
        project_root=tmp_path,
        events_path=tmp_path / "events.jsonl",
    )
    ctx.quest_context = QuestContext(
        id="test01", title="Test Quest", project_root=tmp_path,
        workflow_dir=project_state_dir(tmp_path) / "quests" / "test-quest" / "workflow",
    )
    return ctx


def _run_bash(ctx, args):
    return anyio.run(get_registry()["bash"].async_invoke, ctx, args)


# The incident's shape: one 28 KB line (far over MATCH_LINE_CAP_BYTES) followed by
# 20 ordinary short lines, all on stdout.
_ONE_LONG_20_SHORT = (
    "python3 -c 'import sys; sys.stdout.write(\"L\" * 28000 + chr(10)); "
    "sys.stdout.writelines(f\"short-{i}\" + chr(10) for i in range(1, 21))'"
)
_ONE_LONG_20_SHORT_RAW = (
    b"L" * 28_000 + b"\n" + b"".join(f"short-{i}\n".encode() for i in range(1, 21))
)
# Three 28 KB lines and nothing else: 84 KB raw that clips down to ~6 KB.
_THREE_LONG_LINES = (
    "python3 -c 'import sys; sys.stdout.write((\"L\" * 28000 + chr(10)) * 3)'"
)
# 40 000 ASCII bytes on EACH stream: both streams exceed their 15 000-byte half,
# so each is cut to exactly half the combined budget.
_FLOOD_BOTH_STREAMS = (
    "python3 -c 'import sys; sys.stdout.write((\"O\" * 99 + chr(10)) * 400); "
    "sys.stderr.write((\"E\" * 99 + chr(10)) * 400)'"
)
# 20 000 + 10 000 == exactly BASH_OUTPUT_CAP_BYTES combined: at the cap, not over.
_EXACT_CAP_BOTH_STREAMS = (
    "python3 -c 'import sys; sys.stdout.write((\"O\" * 99 + chr(10)) * 200); "
    "sys.stderr.write((\"E\" * 99 + chr(10)) * 100)'"
)
# 200 lines of 100 U+00E9 = 40 200 bytes but only 20 200 characters, in 201-byte
# lines that do NOT divide the 30 000-byte budget, so the cut lands mid-sequence.
_MULTIBYTE_FLOOD = (
    "python3 -c 'import sys; sys.stdout.buffer.write("
    "((chr(233) * 100 + chr(10)) * 200).encode(\"utf-8\"))'"
)
# A 5-byte stderr beside a 100 000-byte stdout flood: the small stream is under
# its half and yields its remainder.
_SMALL_ERR_BESIDE_FLOOD = (
    "python3 -c 'import sys; sys.stderr.write(\"boom\" + chr(10)); "
    "sys.stdout.write((\"A\" * 99 + chr(10)) * 1000)'"
)


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


def _active_policy(tmp_path: Path):
    from imp.engine.sandbox import SandboxPolicy
    return SandboxPolicy(
        active=True, worktree=str(tmp_path),
        git_common_dir=str(tmp_path / ".git"), git_ro_remask=(),
        writable_caches=(), extra_readable=(), extra_writable=(),
        secret_dirs=(), secret_files=(), child_env=(), network=True, note="")


_SANDBOX_NOTE_TEXT = (
    "a write was refused by imp's sandbox, not by the OS at large: shared git "
    "refs (all but `refs/heads/imp/`), the main `.git/index`, and paths outside "
    "this quest's worktree are read-only. Retrying will not help — if the change "
    "belongs in the user's checkout, hand the user the command to run themselves."
)

# Real refusal shapes a masked refs/heads or .git/index produces, plus case
# variants: matching is case-insensitive.
_DENIED_WRITE_STDERR = (
    "fatal: update_ref failed for ref 'refs/heads/main': cannot lock ref "
    "'refs/heads/main': Unable to create '/x/.git/refs/heads/main.lock': "
    "Read-only file system",
    "error: cannot lock ref 'refs/heads/main': unable to resolve reference",
    "fatal: unable to write new index file",
    "FATAL: COULD NOT WRITE NEW INDEX FILE",
    "error: Cannot Lock Ref 'refs/heads/main'",
    "cp: cannot create regular file '/x/y': READ-ONLY FILE SYSTEM",
)


def _sandboxed_sh(monkeypatch: pytest.MonkeyPatch, script: str) -> None:
    """Run `script` through the REAL wrapper minus bwrap: the trailing
    `sh -c <sentinel script> <script>` argv build_sandbox_spawn emits. The tool
    believes the sandbox is active, the entry marker is emitted by the outer
    shell, and these tests move with the wrapper instead of re-encoding it."""
    import imp.agents.tools.bash as bashmod
    from tests.test_sandbox import wrapper_argv
    monkeypatch.setattr(
        bashmod, "build_sandbox_spawn",
        lambda command, **kw: SandboxSpawn(argv=wrapper_argv(script),
                                           env=dict(os.environ)))


@pytest.mark.parametrize("message", _DENIED_WRITE_STDERR)
def test_bash_denied_write_carries_sandbox_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, message: str,
) -> None:
    # AC 3: active policy + nonzero exit + a mask-shaped stderr marker in any
    # casing => the verbatim note BESIDE the untouched OS error.
    _sandboxed_sh(monkeypatch, f"echo {shlex.quote(message)} >&2; exit 128")
    ctx = _ctx(tmp_path)
    ctx.base_sandbox_policy = _active_policy(tmp_path)
    out = json.loads(_run_bash(
        ctx, {"command": "git update-ref refs/heads/main HEAD"}))
    assert out["returncode"] == 128
    assert out["sandbox_note"] == _SANDBOX_NOTE_TEXT
    assert message in out["stderr"]     # the raw OS error is still reported


def test_bash_no_sandbox_note_on_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # AC 4a: a marker on a command that SUCCEEDED is not a denial.
    _sandboxed_sh(monkeypatch, "echo 'warning: Read-only file system' >&2; exit 0")
    ctx = _ctx(tmp_path)
    ctx.base_sandbox_policy = _active_policy(tmp_path)
    out = json.loads(_run_bash(ctx, {"command": "x"}))
    assert out["returncode"] == 0
    assert "sandbox_note" not in out


def test_bash_no_sandbox_note_without_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # AC 4b: `Permission denied` is deliberately NOT a marker — it is common for
    # unrelated reasons (non-executable file, missing tool).
    _sandboxed_sh(monkeypatch, "echo 'sh: ./x: Permission denied' >&2; exit 126")
    ctx = _ctx(tmp_path)
    ctx.base_sandbox_policy = _active_policy(tmp_path)
    out = json.loads(_run_bash(ctx, {"command": "./x"}))
    assert out["returncode"] == 126
    assert "Permission denied" in out["stderr"]
    assert "sandbox_note" not in out


def test_bash_no_sandbox_note_when_policy_inactive(tmp_path: Path) -> None:
    # AC 4c: policy present but inactive -> bare ["sh","-c",cmd] argv, no note.
    import dataclasses
    ctx = _ctx(tmp_path)
    ctx.base_sandbox_policy = dataclasses.replace(
        _active_policy(tmp_path), active=False)
    out = json.loads(_run_bash(
        ctx, {"command": "echo 'cannot lock ref refs/heads/main' >&2; exit 1"}))
    assert out["returncode"] == 1
    assert "cannot lock ref" in out["stderr"]
    assert "sandbox_note" not in out


def test_bash_no_sandbox_note_without_policy(tmp_path: Path) -> None:
    # AC 4c: no policy at all — an unsandboxed EROFS is the OS's, not imp's.
    out = json.loads(_run_bash(
        _ctx(tmp_path),
        {"command": "echo 'x: Read-only file system' >&2; exit 1"}))
    assert out["returncode"] == 1
    assert "sandbox_note" not in out


def test_bash_cap_killed_command_carries_no_sandbox_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # AC 4d: a cap-kill forces the EMITTED returncode to 0, so the note keys on
    # that value and not on proc.returncode (-9). Bounded emitter (84 KB, then it
    # blocks in sleep) so the poller kills deterministically, no write race.
    import imp.agents.tools.bash as bash_mod
    from imp.agents.tools import limits
    monkeypatch.setattr(limits, "BG_STREAM_FILE_CAP_BYTES", 50_000)
    monkeypatch.setattr(bash_mod, "_FG_CAP_POLL_S", 0.05)
    _sandboxed_sh(
        monkeypatch,
        "echo 'fatal: cannot lock ref refs/heads/main' >&2; "
        f"{_THREE_LONG_LINES}; sleep 30")
    ctx = _ctx(tmp_path)
    ctx.base_sandbox_policy = _active_policy(tmp_path)

    async def go() -> dict:
        with anyio.fail_after(60):      # deadlock backstop only
            raw = await get_registry()["bash"].async_invoke(ctx, {"command": "x"})
            return json.loads(raw)

    out = anyio.run(go)
    assert out["returncode"] == 0       # cap-kill is a NORMAL result
    assert "sandbox_note" not in out


def test_bash_sandbox_note_fires_past_display_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # AC 5: the note is derived from the FULL post-strip stderr. 40 KB of filler
    # pushes the refusal past the 30 KB display cut, so the RETURNED stderr no
    # longer carries it — the note must still fire.
    _sandboxed_sh(
        monkeypatch,
        "python3 -c 'import sys; sys.stderr.write((\"E\" * 99 + chr(10)) * 400)'; "
        "echo 'fatal: cannot lock ref refs/heads/main: Read-only file system' >&2; "
        "exit 128")
    ctx = _ctx(tmp_path)
    ctx.base_sandbox_policy = _active_policy(tmp_path)
    out = json.loads(_run_bash(
        ctx, {"command": "git update-ref refs/heads/main HEAD"}))
    assert out["returncode"] == 128
    assert "cannot lock ref" not in out["stderr"]   # cut by the 30 KB cap
    assert "30 KB cap" in out["notice"]
    assert out["sandbox_note"] == _SANDBOX_NOTE_TEXT


def test_bash_reports_sandbox_spawn_failure_distinctly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import imp.agents.tools.bash as bashmod
    # Simulate a sandboxed spawn that failed to ENTER: an argv that exits nonzero
    # and never emits the entry sentinel (no real bwrap). Every attempt fails, so
    # the tool retries to exhaustion and returns the setup-failure result.
    monkeypatch.setattr(
        bashmod, "build_sandbox_spawn",
        lambda command, **kw: SandboxSpawn(argv=["sh", "-c", "exit 3"],
                                           env=dict(os.environ)))
    monkeypatch.setattr(bashmod, "sandbox_spawn_backoff_delay", lambda attempt: 0.0)
    ctx = _ctx(tmp_path)
    ctx.base_sandbox_policy = _active_policy(tmp_path)
    out = json.loads(_run_bash(ctx, {"command": "x"}))
    assert out["status"] == "sandbox_setup_failed"
    assert "did NOT run" in out["error"]
    assert out["returncode"] == 3


def test_bash_exit_122_without_marker_is_a_plain_result(tmp_path: Path) -> None:
    # Detection is marker-driven, not returncode-driven: exit 122 alone is an
    # ordinary command exit, not a sandbox-setup failure.
    out = json.loads(_run_bash(_ctx(tmp_path), {"command": "exit 122"}))
    assert "status" not in out
    assert out["returncode"] == 122


def test_bash_syntax_error_is_a_plain_result_not_a_spawn_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # AC 1/2: an unterminated backquote cannot suppress the sentinel, so the
    # command's own exit 2 and stderr are reported and NOTHING is retried.
    from imp.engine.sandbox import SANDBOX_ENTERED_MARKER
    _sandboxed_sh(monkeypatch, 'echo "`')
    ctx = _ctx(tmp_path)
    ctx.base_sandbox_policy = _active_policy(tmp_path)
    ctx.event_log = _RecLog()
    out = json.loads(_run_bash(ctx, {"command": 'echo "`'}))
    assert "status" not in out                       # not a sandbox-setup failure
    assert "error" not in out                        # no "did NOT run" claim
    assert out["returncode"] == 2                    # the shell's real exit code
    assert out["stderr"].strip() != ""               # its own diagnosis survives
    assert SANDBOX_ENTERED_MARKER not in out["stderr"]
    assert ctx.event_log.warnings() == []            # spawned exactly once


_BWRAP_ERR = "bwrap: Can't bind mount /nope: No such file or directory"


def test_bash_sandbox_spawn_failure_emits_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import imp.agents.tools.bash as bashmod
    script = f"printf '%s\\n' {shlex.quote(_BWRAP_ERR)} >&2; exit 3"
    monkeypatch.setattr(
        bashmod, "build_sandbox_spawn",
        lambda command, **kw: SandboxSpawn(argv=["sh", "-c", script],
                                           env=dict(os.environ)))
    monkeypatch.setattr(bashmod, "sandbox_spawn_backoff_delay", lambda attempt: 0.0)
    ctx = _ctx(tmp_path)
    ctx.base_sandbox_policy = _active_policy(tmp_path)
    ctx.event_log = _RecLog()
    json.loads(_run_bash(ctx, {"command": "x"}))
    warns = ctx.event_log.warnings()
    # 4 retry breadcrumbs (attempts 2..5) then the terminal setup-failure warning.
    assert [w["reason"] for w in warns] == ["sandbox-spawn-failed"] * 5
    assert "retrying (2/5)" in warns[0]["message"]
    assert "retrying (5/5)" in warns[3]["message"]
    # AC 5a: bwrap's reason rides BOTH foreground emit paths — every retry
    # breadcrumb and the terminal overlay-setup warning.
    assert [w["detail"] for w in warns] == [_BWRAP_ERR] * 5


def test_bash_retries_then_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `bash()` builds `argv` ONCE (above the retry region) and the
    # loop re-spawns that same argv on each attempt — exactly as production absorbs
    # a transient bwrap userns failure. So the retry variation must be driven
    # through the seam the loop re-invokes (the spawn itself), NOT build_sandbox_spawn
    # (off the retry path — called once). A stateful counter-file argv makes the
    # fixed command fail (no marker, exit 3) on the first two spawns and enter the
    # sandbox (emit the marker) on the 3rd.
    import shlex

    import imp.agents.tools.bash as bashmod
    from imp.engine.sandbox import SANDBOX_ENTERED_MARKER
    counter = tmp_path / "spawn-count"
    q = shlex.quote(str(counter))
    script = (
        f"n=$(cat {q} 2>/dev/null || echo 0); n=$((n+1)); "
        f'echo "$n" > {q}; '
        f'if [ "$n" -ge 3 ]; then '
        f"printf '%s\\n' {SANDBOX_ENTERED_MARKER} >&2; echo done; "
        f"else exit 3; fi")
    monkeypatch.setattr(
        bashmod, "build_sandbox_spawn",
        lambda command, **kw: SandboxSpawn(argv=["sh", "-c", script],
                                           env=dict(os.environ)))
    monkeypatch.setattr(bashmod, "sandbox_spawn_backoff_delay", lambda attempt: 0.0)
    ctx = _ctx(tmp_path)
    ctx.base_sandbox_policy = _active_policy(tmp_path)
    ctx.event_log = _RecLog()
    out = json.loads(_run_bash(ctx, {"command": "x"}))
    assert "status" not in out                           # healthy on the 3rd attempt
    assert out["returncode"] == 0
    assert "done" in out["stdout"]
    assert counter.read_text().strip() == "3"            # 2 failed spawns + 1 success
    warns = ctx.event_log.warnings()
    assert [w["reason"] for w in warns] == ["sandbox-spawn-failed"] * 2
    assert "retrying (2/5)" in warns[0]["message"]
    assert "retrying (3/5)" in warns[1]["message"]


def test_bash_healthy_sandboxed_command_strips_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import imp.agents.tools.bash as bashmod
    from imp.engine.sandbox import SANDBOX_ENTERED_MARKER
    monkeypatch.setattr(
        bashmod, "build_sandbox_spawn",
        lambda command, **kw: SandboxSpawn(
            argv=["sh", "-c",
                  f"printf '%s\\n' {SANDBOX_ENTERED_MARKER} >&2; echo real >&2; "
                  "echo out"],
            env=dict(os.environ)))
    ctx = _ctx(tmp_path)
    ctx.base_sandbox_policy = _active_policy(tmp_path)
    out = json.loads(_run_bash(ctx, {"command": "x"}))
    assert "status" not in out                          # healthy result
    assert SANDBOX_ENTERED_MARKER not in out["stderr"]  # marker stripped
    assert "real" in out["stderr"]
    assert out["returncode"] == 0


def test_bash_bare_command_never_flagged_spawn_failure(tmp_path: Path) -> None:
    # No policy -> bare -> sandbox_active False -> a nonzero exit is a plain result.
    out = json.loads(_run_bash(_ctx(tmp_path), {"command": "exit 3"}))
    assert "status" not in out
    assert out["returncode"] == 3


def test_bash_inactive_policy_never_flagged_spawn_failure(tmp_path: Path) -> None:
    # policy present but active=False -> guard is False -> nonzero exit is a plain result.
    import dataclasses
    pol = dataclasses.replace(_active_policy(tmp_path), active=False)
    ctx = _ctx(tmp_path)
    ctx.base_sandbox_policy = pol
    out = json.loads(_run_bash(ctx, {"command": "exit 3"}))
    assert "status" not in out
    assert out["returncode"] == 3


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
        with anyio.fail_after(30):  # deadlock backstop only
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
        with anyio.fail_after(30):  # deadlock backstop only
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
        with anyio.fail_after(30):  # deadlock backstop only
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
        child_env=(("PATH", "/usr/bin"),), network=True, note="",
    )
    # _run_bash propagates the RuntimeError out via anyio.run
    with pytest.raises(RuntimeError):
        _run_bash(ctx, {"command": "echo hi"})
    assert captured["argv"][0] == "bwrap"
    assert captured["argv"][-4:-2] == ["sh", "-c"]
    assert captured["argv"][-1] == "echo hi"


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
    # clock.
    class _Coord:
        should_pause = False

    coord = _Coord()
    ctx = _ctx(tmp_path)
    ctx.coordinator = coord
    holder: dict[str, dict] = {}

    async def _scenario() -> None:
        with anyio.fail_after(30):  # deadlock backstop only
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
        with anyio.fail_after(30):  # deadlock backstop only
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


def test_bash_prepends_shim_dir_to_path(tmp_path: Path) -> None:
    ctx = ToolContext(project_root=tmp_path, events_path=tmp_path / "events.jsonl")
    ctx.quest_context = QuestContext(
        id="test01", title="Test Quest", project_root=tmp_path,
        workflow_dir=project_state_dir(tmp_path) / "quests" / "test-quest" / "workflow",
        quest_home=str(project_state_dir(tmp_path) / "quests" / "test-quest"),
    )
    out = json.loads(_run_bash(ctx, {"command": 'printf %s "$PATH"'}))
    shim = project_state_dir(tmp_path) / "quests" / "test-quest" / "imp-shim"
    assert out["stdout"].startswith(str(shim) + os.pathsep)


def test_bash_no_quest_home_leaves_path_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", os.pathsep.join(
        p for p in os.environ.get("PATH", "").split(os.pathsep) if "imp-shim" not in p))
    ctx = ToolContext(project_root=tmp_path, events_path=tmp_path / "events.jsonl")
    ctx.quest_context = QuestContext(
        id="test01", title="Test Quest", project_root=tmp_path,
        workflow_dir=project_state_dir(tmp_path) / "quests" / "test-quest" / "workflow",
    )
    out = json.loads(_run_bash(ctx, {"command": 'printf %s "$PATH"'}))
    assert "imp-shim" not in out["stdout"]


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
    # AC 4: foreground spawns with stdin=DEVNULL (via the shared spawn-to-files
    # core), not the old pipe path's inherited stdin. Redirect this process's own
    # fd 0 at a real file for the duration of the call so the assertion cannot
    # pass vacuously when the runner's fd 0 is already /dev/null (headless CI): an
    # inherited-stdin regression would surface that real file in the child's
    # readlink and echo its bytes through `cat`, while DEVNULL keeps fd 0 at
    # /dev/null and yields instant EOF.
    stdin_file = tmp_path / "real_stdin"
    stdin_file.write_text("stolen-stdin-marker\n")
    saved_fd0 = os.dup(0)
    with open(stdin_file, "rb") as f:
        os.dup2(f.fileno(), 0)
    try:
        out = json.loads(_run_bash(_ctx(tmp_path),
                                   {"command": "readlink /proc/self/fd/0; cat"}))
    finally:
        os.dup2(saved_fd0, 0)
        os.close(saved_fd0)
    assert out["returncode"] == 0
    # readlink prints /dev/null (not the redirected real file), and `cat` draining
    # a DEVNULL stdin hits instant EOF and echoes none of the file's bytes.
    assert out["stdout"].strip() == "/dev/null"
    assert "stolen-stdin-marker" not in out["stdout"]


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
        with anyio.fail_after(30):  # deadlock backstop only
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
        with anyio.fail_after(30):  # deadlock backstop only
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


def test_bash_description_describes_promotion_not_reaping() -> None:
    # AC 10: the tool the model sees no longer says a foreground command is
    # "reaped after 120 s"; it describes promotion to a background task
    # readable/killable via bash_output/bash_kill, and still states the
    # 120 s default / 600 s cap contract.
    desc = get_registry()["bash"].description
    assert "reaped" not in desc
    assert "promoted to a background task" in desc
    assert "bash_output" in desc and "bash_kill" in desc
    assert "120" in desc and "600" in desc


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
        with anyio.fail_after(30):  # deadlock backstop only
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


def test_bash_notice_clause_wording_for_clip_and_cap() -> None:
    # AC 4: the three shapes verbatim, plus the singular form. These literals are
    # bound to MATCH_LINE_CAP_BYTES / BASH_OUTPUT_CAP_BYTES (F4 note in limits.py).
    from imp.agents.tools.bash import _clip_cap_notice_clause as clause
    assert clause(0, False) is None      # nothing fired -> nothing to disclose
    assert clause(0, True) == "Output truncated at the 30 KB cap."
    assert clause(1, False) == "1 long line clipped at 2000 chars."
    assert clause(3, False) == "3 long lines clipped at 2000 chars."
    assert clause(3, True) == (
        "3 long lines clipped at 2000 chars, then output truncated at the 30 KB cap.")
    assert clause(1, True) == (
        "1 long line clipped at 2000 chars, then output truncated at the 30 KB cap.")


def test_bash_clips_long_line_and_keeps_short_lines_intact(tmp_path: Path) -> None:
    # AC 1: the 28 KB line comes back as exactly MATCH_LINE_CAP_BYTES chars plus the
    # marker; every short line is untouched. Post-clip the whole result is well under
    # the 30 KB cap, so nothing else is dropped — the point of clipping BEFORE capping.
    ctx = _ctx(tmp_path)
    out = json.loads(_run_bash(ctx, {"command": _ONE_LONG_20_SHORT}))
    lines = out["stdout"].split("\n")
    assert lines[0] == "L" * MATCH_LINE_CAP_BYTES + "…[truncated]"
    assert lines[1:] == [f"short-{i}" for i in range(1, 21)] + [""]
    assert out["returncode"] == 0
    assert out["notice"].startswith("1 long line clipped at 2000 chars. [")


def test_bash_spill_holds_the_unclipped_output(tmp_path: Path) -> None:
    # AC 2: single stdout stream, empty stderr, ASCII -> the spill file is
    # byte-identical to what the command emitted (unlabeled, unclipped, uncapped).
    ctx = _ctx(tmp_path)
    out = json.loads(_run_bash(ctx, {"command": _ONE_LONG_20_SHORT}))
    assert "notice" in out
    spilled = list((tmp_path / "tool-results").iterdir())
    assert len(spilled) == 1
    assert spilled[0].read_bytes() == _ONE_LONG_20_SHORT_RAW


def test_bash_output_without_long_lines_is_untouched_and_unannounced(
    tmp_path: Path,
) -> None:
    # AC 3: no over-long line and under the cap -> byte-identical text (trailing
    # newline and final unterminated line both preserved) and NO notice key at all.
    ctx = _ctx(tmp_path)
    out = json.loads(_run_bash(ctx, {"command": "printf 'a\\nb\\n'"}))
    assert out["stdout"] == "a\nb\n"
    assert "notice" not in out
    out2 = json.loads(_run_bash(ctx, {"command": "printf 'a\\nb'"}))
    assert out2["stdout"] == "a\nb"
    assert "notice" not in out2
    assert not (tmp_path / "tool-results").exists()


def test_bash_cap_only_notice_names_the_cap(tmp_path: Path) -> None:
    # AC 4 (cap-only): short lines flooding past 30 KB -> the cap clause alone.
    ctx = _ctx(tmp_path)
    out = json.loads(_run_bash(ctx, {"command": "yes ABCDEFGHIJ | head -c 100000"}))
    assert out["notice"].startswith("Output truncated at the 30 KB cap. [")
    assert len(out["stdout"]) == 30_000


def test_bash_clip_then_cap_notice_reports_both(tmp_path: Path) -> None:
    # AC 4 (both): three 28 KB lines clip down to ~6 KB, and the 40 KB short-line
    # flood still puts the POST-CLIP text over the cap -> both clauses, in order.
    ctx = _ctx(tmp_path)
    cmd = f"{_THREE_LONG_LINES}; yes ABCDEFGHIJ | head -c 40000"
    out = json.loads(_run_bash(ctx, {"command": cmd}))
    assert out["notice"].startswith(
        "3 long lines clipped at 2000 chars, then output truncated at the 30 KB cap. [")
    assert len(out["stdout"].encode("utf-8")) == 30_000
    # The head carries three U+2026 clip markers (3 bytes, 1 char each), so a
    # 30 000-BYTE head is 29 994 characters.
    assert len(out["stdout"]) == 29_994
    assert out["stdout"].startswith("L" * MATCH_LINE_CAP_BYTES + "…[truncated]")


def test_bash_midcommand_pause_tombstone_reports_clipped_output(tmp_path: Path) -> None:
    # AC 5a: the pause tombstone keeps its status/kill_reason but carries the SAME
    # clipped text and notice as any other branch.
    class _Coord:
        should_pause = False

    coord = _Coord()
    ctx = _ctx(tmp_path)
    ctx.coordinator = coord
    holder: dict[str, dict] = {}

    async def _scenario() -> None:
        with anyio.fail_after(30):      # deadlock backstop only
            async with anyio.create_task_group() as tg:
                async def _runner() -> None:
                    holder["out"] = json.loads(
                        await get_registry()["bash"].async_invoke(
                            ctx, {"command": f"{_ONE_LONG_20_SHORT}; sleep 30"}))

                async def _pauser() -> None:
                    from imp.agents.tools.bg import _bg_dir
                    d = _bg_dir(ctx)
                    # Poll for the long line to actually land: a fixed sleep would
                    # race a loaded machine and pause before the write.
                    while not any(p.stat().st_size >= 28_000
                                  for p in d.glob("fg-*.out")):
                        await anyio.sleep(0.05)
                    coord.should_pause = True

                tg.start_soon(_runner)
                tg.start_soon(_pauser)

    anyio.run(_scenario)
    out = holder["out"]
    assert out["status"] == "killed"
    assert out["kill_reason"] == "interrupted"
    assert out["stdout"].split("\n")[0] == "L" * MATCH_LINE_CAP_BYTES + "…[truncated]"
    assert out["notice"].startswith("1 long line clipped at 2000 chars. [")
    assert ctx.running_processes == []


def test_bash_cap_killed_runaway_reports_clipped_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # AC 5a: the own-cap kill branch (returncode massaged to 0) also reports clipped
    # text and discloses the alteration. The emitter is BOUNDED (84 KB, then it
    # blocks in sleep): that already exceeds the shrunken 50 000-byte ceiling, so the
    # poller kills it deterministically without an unbounded write race.
    import imp.agents.tools.bash as bash_mod
    from imp.agents.tools import limits
    monkeypatch.setattr(limits, "BG_STREAM_FILE_CAP_BYTES", 50_000)
    monkeypatch.setattr(bash_mod, "_FG_CAP_POLL_S", 0.05)
    ctx = _ctx(tmp_path)

    async def go() -> dict:
        with anyio.fail_after(30):      # deadlock backstop only
            raw = await get_registry()["bash"].async_invoke(
                ctx, {"command": f"{_THREE_LONG_LINES}; sleep 30"})
            return json.loads(raw)

    out = anyio.run(go)
    assert out["returncode"] == 0                     # cap-kill is a NORMAL result
    assert out["stdout"].split("\n")[0] == "L" * MATCH_LINE_CAP_BYTES + "…[truncated]"
    # The kill can land while the 84 KB is still being written, so the exact line
    # COUNT is not deterministic here (the exact wordings are pinned in
    # test_bash_notice_clause_wording_for_clip_and_cap); what is deterministic is
    # that the kill needs > 50 KB on disk, hence at least one over-long line.
    assert "clipped at 2000 chars" in out["notice"]
    assert ctx.running_processes == []


def test_bash_sandbox_setup_failure_clips_and_discloses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # AC 5b: the sandbox-setup-failure branch returns the same clipped text with the
    # same notice + spill as any other branch — no branch returns altered output
    # without disclosing it. AC 5c: the four RETRIED attempts (whose output is
    # discarded) write no spill file, so exactly one file exists.
    import imp.agents.tools.bash as bashmod
    monkeypatch.setattr(
        bashmod, "build_sandbox_spawn",
        lambda command, **kw: SandboxSpawn(
            argv=["sh", "-c", "python3 -c 'print(\"L\" * 28000)'; exit 3"],
            env=dict(os.environ)))
    monkeypatch.setattr(bashmod, "sandbox_spawn_backoff_delay", lambda attempt: 0.0)
    ctx = _ctx(tmp_path)
    ctx.base_sandbox_policy = _active_policy(tmp_path)
    out = json.loads(_run_bash(ctx, {"command": "x"}))
    assert out["status"] == "sandbox_setup_failed"
    assert out["stdout"].split("\n")[0] == "L" * MATCH_LINE_CAP_BYTES + "…[truncated]"
    assert out["notice"].startswith("1 long line clipped at 2000 chars. [")
    spilled = list((tmp_path / "tool-results").iterdir())
    assert len(spilled) == 1
    assert spilled[0].read_bytes() == b"L" * 28_000 + b"\n"


def test_bash_clips_long_line_on_stderr_and_sums_cross_stream_count(
    tmp_path: Path,
) -> None:
    # Pins that stderr is clipped (not just stdout) and the clip count is cross-stream.
    ctx = _ctx(tmp_path)
    cmd = (
        "python3 -c 'import sys; sys.stdout.write(\"L\" * 28000 + chr(10)); "
        "sys.stderr.write(\"E\" * 28000 + chr(10))'"
    )
    out = json.loads(_run_bash(ctx, {"command": cmd}))
    assert out["stdout"].split("\n")[0] == "L" * MATCH_LINE_CAP_BYTES + "…[truncated]"
    assert out["stderr"].split("\n")[0] == "E" * MATCH_LINE_CAP_BYTES + "…[truncated]"
    assert out["notice"].startswith("2 long lines clipped at 2000 chars. [")


def test_cap_combined_under_cap_returns_inputs_untouched() -> None:
    from imp.agents.tools.bash import _cap_combined
    out, err, over = _cap_combined("a" * 20_000, "b" * 9_999)
    assert (out, err, over) == ("a" * 20_000, "b" * 9_999, False)


def test_cap_combined_exact_cap_is_inside_the_inclusive_boundary() -> None:
    # 20 000 + 10 000 == BASH_OUTPUT_CAP_BYTES: at the cap is UNDER the cap
    # (today's `>` comparison), and one byte more trips it.
    from imp.agents.tools.bash import _cap_combined
    out, err, over = _cap_combined("a" * 20_000, "b" * 10_000)
    assert over is False
    assert len(out) == 20_000
    assert len(err) == 10_000
    _, _, over_one_more = _cap_combined("a" * 20_001, "b" * 10_000)
    assert over_one_more is True


def test_cap_combined_both_over_half_splits_the_budget() -> None:
    from imp.agents.tools.bash import _cap_combined
    out, err, over = _cap_combined("a" * 40_000, "b" * 40_000)
    assert over is True
    assert len(out.encode("utf-8")) == 15_000
    assert len(err.encode("utf-8")) == 15_000


def test_cap_combined_small_stream_yields_its_remainder() -> None:
    # Fair share WITH reallocation: a stream at or under its half is returned
    # whole and the other one gets 30 000 minus that stream's byte length.
    from imp.agents.tools.bash import _cap_combined
    out, err, over = _cap_combined("boom\n", "b" * 40_000)
    assert over is True
    assert out == "boom\n"
    assert len(err.encode("utf-8")) == 29_995
    # The lone flooder still gets the WHOLE budget when the other is empty.
    out2, err2, over2 = _cap_combined("a" * 40_000, "")
    assert over2 is True
    assert len(out2) == 30_000
    assert err2 == ""


def test_cap_combined_multibyte_cut_lands_at_a_character_boundary() -> None:
    # 3 ASCII bytes beside a U+00E9 flood: the flooder's allocation is
    # 30 000 - 3 == 29 997, an ODD budget over 2-byte characters, so the cut
    # falls MID-SEQUENCE. errors="ignore" drops the one dangling byte, adds no
    # U+FFFD, and lands the stream 1 byte under its allocation.
    from imp.agents.tools.bash import _cap_combined
    out, err, over = _cap_combined("AAA", "é" * 20_000)
    assert over is True
    assert out == "AAA"
    assert len(err.encode("utf-8")) == 29_996
    assert "�" not in err
    assert err == "é" * 14_998


def test_cap_combined_cuts_multibyte_under_the_char_count_but_over_in_bytes() -> None:
    # 20 000 CHARACTERS (under 30 000) but 40 000 BYTES (over) — the case the old
    # per-stream character slice let through untouched.
    from imp.agents.tools.bash import _cap_combined
    src = "é" * 20_000
    out, err, over = _cap_combined(src, "")
    assert over is True
    assert out != src
    assert len(out.encode("utf-8")) == 30_000
    assert out == "é" * 15_000
    assert err == ""


def test_bash_both_streams_flooded_share_the_combined_cap(tmp_path: Path) -> None:
    # AC 1: 40 KB on each stream -> 15 000 displayed bytes each (30 000 combined),
    # the cap clause, and a spill carrying BOTH streams in full.
    ctx = _ctx(tmp_path)
    out = json.loads(_run_bash(ctx, {"command": _FLOOD_BOTH_STREAMS}))
    n_out = len(out["stdout"].encode("utf-8"))
    n_err = len(out["stderr"].encode("utf-8"))
    assert n_out == 15_000
    assert n_err == 15_000
    assert n_out + n_err == 30_000
    assert out["notice"].startswith("Output truncated at the 30 KB cap. [")
    spilled = list((tmp_path / "tool-results").iterdir())
    assert len(spilled) == 1
    content = spilled[0].read_text(encoding="utf-8")
    assert content.count("O" * 99 + "\n") == 400      # FULL stdout recoverable
    assert content.count("E" * 99 + "\n") == 400      # FULL stderr recoverable


def test_bash_small_stream_beside_a_flood_keeps_its_bytes(tmp_path: Path) -> None:
    # AC 4: the 5-byte stderr survives byte-for-byte and the flooder gets
    # 30 000 - 5 bytes, so the combined display is exactly the cap.
    ctx = _ctx(tmp_path)
    out = json.loads(_run_bash(ctx, {"command": _SMALL_ERR_BESIDE_FLOOD}))
    assert out["stderr"] == "boom\n"
    assert len(out["stdout"].encode("utf-8")) == 29_995
    assert len(out["stdout"].encode("utf-8")) + len(out["stderr"].encode("utf-8")) == 30_000
    assert out["notice"].startswith("Output truncated at the 30 KB cap. [")


def test_bash_multibyte_flood_cuts_on_bytes_at_a_char_boundary(tmp_path: Path) -> None:
    # AC 5 + AC 6: 40 200 bytes / 20 200 chars — over the cap in BYTES while under
    # it in characters, which the old character slice let through untouched. The
    # 201-byte lines leave the 30 000-byte cut mid-sequence: the dangling byte is
    # dropped (1 byte under the budget) and no U+FFFD appears.
    ctx = _ctx(tmp_path)
    out = json.loads(_run_bash(ctx, {"command": _MULTIBYTE_FLOOD}))
    assert out["notice"].startswith("Output truncated at the 30 KB cap. [")
    assert len(out["stdout"].encode("utf-8")) == 29_999
    assert len(out["stdout"]) == 15_074
    assert "�" not in out["stdout"]
    assert out["stdout"].startswith("é" * 100 + "\n")
    spilled = list((tmp_path / "tool-results").iterdir())
    assert len(spilled) == 1
    assert len(spilled[0].read_bytes()) == 40_200      # FULL output recoverable


def test_bash_exactly_at_the_combined_cap_is_byte_exact_and_unannounced(
    tmp_path: Path,
) -> None:
    # AC 7 + AC 8: a two-stream total of exactly 30 000 bytes is UNDER the cap —
    # both streams byte-exact, no notice key at all, no spill file.
    ctx = _ctx(tmp_path)
    out = json.loads(_run_bash(ctx, {"command": _EXACT_CAP_BOTH_STREAMS}))
    assert len(out["stdout"]) == 20_000
    assert len(out["stderr"]) == 10_000
    assert "notice" not in out
    assert not (tmp_path / "tool-results").exists()


def test_subagent_quest_dir_makes_imp_quest_dir_absolute(tmp_path: Path) -> None:
    # A subagent has no live ctx.quest, so bash falls back to the QuestContext
    # snapshot. quest_dir is the host path; quest_home stays the render string.
    home = tmp_path / "state" / "quests" / "test-quest"
    home.mkdir(parents=True)
    ctx = ToolContext(project_root=tmp_path, events_path=tmp_path / "events.jsonl")
    ctx.quest_context = QuestContext(
        id="test01", title="Test Quest", project_root=tmp_path,
        workflow_dir=home / "workflow",
        quest_home=".imp/quests/test-quest", quest_dir=home,
    )
    out = json.loads(_run_bash(ctx, {"command": 'printf %s "$IMP_QUEST_DIR"'}))
    assert out["stdout"] == str(home)


def _secret_policy(tmp_path: Path):
    from imp.engine.sandbox import SandboxPolicy
    return SandboxPolicy(
        active=True, worktree=str(tmp_path), git_common_dir=str(tmp_path / ".git"),
        git_ro_remask=(), writable_caches=(), extra_readable=(), extra_writable=(),
        secret_dirs=(), secret_files=(),
        child_env=(("PATH", "/usr/bin"),), network=True, note="")


def test_bash_foreground_spawns_with_the_sandbox_child_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # AC 6(a): `echo $IMP_TELEGRAM_TOKEN` inside the sandbox was the demonstrated leak;
    # the foreground spawn must use the env paired with the argv, not a locally
    # composed overlay of os.environ.
    import imp.agents.tools.bash as bashmod

    monkeypatch.setenv("IMP_TELEGRAM_TOKEN", "canary-42:secret")
    captured: dict[str, object] = {}

    async def fake_open_process(argv: list[str], **kw: object) -> object:
        captured["env"] = kw.get("env")
        raise RuntimeError("stop after capture")

    monkeypatch.setattr(bashmod.anyio, "open_process", fake_open_process)
    ctx = _ctx(tmp_path)
    ctx.base_sandbox_policy = _secret_policy(tmp_path)
    with pytest.raises(RuntimeError):
        _run_bash(ctx, {"command": "echo hi"})
    env = captured["env"]
    assert "IMP_TELEGRAM_TOKEN" not in env
    assert env["PATH"] == "/usr/bin"
    assert env["IMP_QUEST_ID"] == "test01"


def test_bash_background_spawns_with_the_sandbox_child_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # AC 6(b): the background path takes the same paired env.
    import imp.agents.tools.bg as bgmod

    monkeypatch.setenv("IMP_TELEGRAM_TOKEN", "canary-42:secret")
    captured: dict[str, object] = {}

    async def fake_spawn_background(ctx, *, command, argv, cwd, env):
        captured["env"] = env
        return "bg1"

    monkeypatch.setattr(bgmod, "spawn_background", fake_spawn_background)
    ctx = _ctx(tmp_path)
    ctx.background_enabled = True
    ctx.base_sandbox_policy = _secret_policy(tmp_path)
    assert _run_bash(ctx, {"command": "echo hi", "run_in_background": True}) == "bg1"
    env = captured["env"]
    assert "IMP_TELEGRAM_TOKEN" not in env
    assert env["PATH"] == "/usr/bin"
    assert env["IMP_QUEST_ID"] == "test01"
