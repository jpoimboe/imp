"""Shared probe machinery for the claude built-in deny-posture guard.

Mechanism: resolve the CLI the SDK actually spawns (its bundled binary, not
whatever `claude` is on PATH), launch it with the exact --disallowedTools
list _build_options produces, and read the `init` system message, where
claude advertises its live tool registry. Anything advertised beyond the
ToolSearch exemption is a built-in the agent could call from every
claude_code step.

Probing under the deny posture rather than bare is load-bearing: some
built-ins only materialize once others are denied, so a baseline probe
never sees them.

probe_advertised_tools has three outcomes and no fourth: it returns the registry
the CLI advertised, skips when the CLI declined to start a session (it exited 0
without writing a byte on every attempt — an environment fault, and the check did
NOT run), or raises. Nothing that never saw an init frame returns, so a dead probe
can never be mistaken for an empty registry.

read_init_frame names which of four exits it took — INIT, EOF, PROCESS_EXITED (the
CLI is gone yet stdout is still open, so a descendant inherited the pipe) or
BUDGET_EXPIRED — and PROCESS_EXITED is what arms the /proc pipe-holder diagnostic.

The probes that spawn the CLI are in tests/test_cc_builtins_drift_live.py;
tests/test_cc_builtins_drift.py drives this module's reader against stubs.
"""

from __future__ import annotations

import contextlib
import json
import os
import selectors
import subprocess
import tempfile
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import ClaudeAgentOptions, CLINotFoundError
from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

from imp.agents.runner import AgentInvocation, Message, QuestContext, TextBlock
from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc

# A hang detector, not a tuned window: ~7x the worst init latency observed under a
# saturated parallel suite, and the pre-init network dial is no longer awaited.
INIT_FRAME_BUDGET = 30.0
# Everything the CLI does before init is external — a git call, an rg, a ps, a
# network dial — so a spawn that yields no frame is retried, stall included.
PROBE_ATTEMPTS = 3
PROBE_RETRY_BACKOFF = 1.0
# Bounds each select wait so proc.poll() is consulted regularly: a CLI that exits
# while a descendant holds our stdout pipe never produces EOF to wake us.
PROC_POLL_INTERVAL = 0.25


class InitReadOutcome(Enum):
    INIT = "init"
    EOF = "eof"
    PROCESS_EXITED = "process_exited"
    BUDGET_EXPIRED = "budget_expired"


@dataclass(frozen=True)
class InitRead:
    frame: dict[str, Any] | None
    stdout_bytes: int
    outcome: InitReadOutcome


@dataclass(frozen=True)
class ProbeAttempt:
    frame: dict[str, Any] | None
    exit_status: int | None      # None => still running at budget expiry
    stdout_bytes: int
    stderr_text: str
    elapsed: float
    pipe_holders: str | None     # set only when the CLI exited without our pipe reaching EOF


def invocation(tmp_path: Path, declared: frozenset[str] = frozenset()) -> AgentInvocation:
    """A real AgentInvocation for _build_options; `declared` rides on top of complete_step."""
    loc = SourceLoc(file=Path("a.md"), line=1)
    agent = AgentDef(
        name="t", description="",
        tools=frozenset({"complete_step"}) | declared,
        subagents=(),
        output_schema=OutputSchema(
            fields=(OutputField(name="status", type="enum",
                                values=("Success",), required=True),),
            source=loc),
        prompt_template_source="hi", source_path=Path("a.md"), source=loc)
    return AgentInvocation(
        agent_def=agent, provider_name="claude_code", model_name="m",
        system_prompt="s",
        initial_messages=(Message(role="user", content=(TextBlock(text="go"),)),),
        quest_context=QuestContext(title="t", project_root=tmp_path,
                                   workflow_dir=tmp_path),
        transcript_dir=tmp_path,
        events_path=tmp_path / "events.jsonl")


def resolve_cli(opts: ClaudeAgentOptions) -> str:
    """The binary the SDK will spawn. Mirrors connect(): an explicit cli_path
    wins, else _find_cli() (bundled → PATH → fixed locations). _find_cli() alone
    ignores cli_path entirely, so both halves are required. The constructor
    spawns nothing."""
    transport = SubprocessCLITransport(prompt="", options=opts)
    try:
        return transport._cli_path or transport._find_cli()
    except CLINotFoundError:
        pytest.skip("claude CLI not found")


def _take_init_frame(buf: bytes) -> tuple[dict[str, Any] | None, bytes]:
    """Consume whole lines from `buf`: the init frame if one parses out, plus the
    unterminated remainder. Non-JSON and non-init lines are skipped."""
    while b"\n" in buf:
        raw, buf = buf.split(b"\n", 1)
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if msg.get("type") == "system" and msg.get("subtype") == "init":
            return msg, buf
    return None, buf


def read_init_frame(proc: subprocess.Popen[bytes], budget: float) -> InitRead:
    """Poll `proc`'s raw stdout fd for the CLI's `init` system message, giving up
    after `budget` seconds. Bytes are buffered and split on newlines, so a frame
    delivered across two writes still parses. Non-JSON and non-init lines are
    skipped. Returns an InitRead naming which of the four exits was taken: INIT
    (frame read), EOF (stdout closed first), PROCESS_EXITED (the CLI is gone yet
    stdout is still open, so a descendant inherited the pipe) or BUDGET_EXPIRED.
    `frame` is set exactly when the outcome is INIT."""
    assert proc.stdout is not None
    fd = proc.stdout.fileno()
    deadline = time.monotonic() + budget
    buf = b""
    stdout_bytes = 0

    with selectors.DefaultSelector() as selector:
        selector.register(proc.stdout, selectors.EVENT_READ)

        def consume() -> InitRead | None:
            """Read one ready chunk; an InitRead once it settles the read, else None."""
            nonlocal buf, stdout_bytes
            chunk = os.read(fd, 65536)
            if not chunk:               # EOF: stdout closed before an init message
                return InitRead(None, stdout_bytes, InitReadOutcome.EOF)
            stdout_bytes += len(chunk)
            buf += chunk
            frame, buf = _take_init_frame(buf)
            if frame is not None:
                return InitRead(frame, stdout_bytes, InitReadOutcome.INIT)
            return None

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return InitRead(None, stdout_bytes, InitReadOutcome.BUDGET_EXPIRED)
            if selector.select(min(remaining, PROC_POLL_INTERVAL)):
                settled = consume()
                if settled is not None:
                    return settled
                continue
            if proc.poll() is None:
                continue
            # The CLI is gone but the pipe is not at EOF. A select timeout only proves
            # nothing was readable at that instant, so drain what landed in the window
            # before the poll: losing those bytes would turn a loud failure into silence.
            # Still deadline-bound — a descendant streaming without end must not
            # outlast the budget inside the one function whose job is bounding a wait.
            while time.monotonic() < deadline and selector.select(0):
                settled = consume()
                if settled is not None:
                    return settled
            return InitRead(None, stdout_bytes, InitReadOutcome.PROCESS_EXITED)


def _pipe_holders(fd: int) -> str:
    """Processes still holding `fd`'s pipe, as `pid N (comm)` joined by ', ', or the
    literal `unknown`. Our own pid is excluded: we hold that same pipe, so every scan
    would otherwise name itself."""
    try:
        pipe_st = os.stat(fd)
        ident = (pipe_st.st_dev, pipe_st.st_ino)
        pids = [name for name in os.listdir("/proc") if name.isdigit()]
    except OSError:
        return "unknown"
    me = str(os.getpid())
    holders: list[str] = []
    for pid in pids:
        if pid == me:
            continue
        try:
            entries = os.listdir(f"/proc/{pid}/fd")
        except OSError:
            continue
        for entry in entries:
            try:
                entry_st = os.stat(f"/proc/{pid}/fd/{entry}")
                if (entry_st.st_dev, entry_st.st_ino) != ident:
                    continue
                comm = Path(f"/proc/{pid}/comm").read_text().strip()
            except OSError:
                continue
            holders.append(f"pid {pid} ({comm})")
            break
    return ", ".join(holders) or "unknown"


def _spawn_and_read_init(cli: str, deny: list[str], cwd: Path) -> ProbeAttempt:
    """One probe spawn, as a ProbeAttempt: what was read, how the CLI ended, how long it
    took, and — only when the CLI exited while our stdout pipe stayed open — which
    processes still hold that pipe. exit_status is None when the CLI outlived the budget."""
    started = time.monotonic()
    user_msg = json.dumps({
        "type": "user",
        "message": {"role": "user", "content": "say ok"},
    })

    # TemporaryFile, not PIPE: nothing drains stderr while we poll stdout for the
    # init frame, so a PIPE could fill its OS buffer and deadlock the CLI.
    with tempfile.TemporaryFile("w+") as stderr_file:
        # Do NOT skip on CLAUDECODE=1: the CLI advertises an identical registry with
        # or without it, and such a skip would hide real drift.
        proc = subprocess.Popen(
            [
                cli, "--print",
                "--input-format", "stream-json",
                "--output-format", "stream-json",
                "--verbose",
                "--setting-sources", "",
                "--no-session-persistence",
                "--strict-mcp-config",
                "--disallowedTools", ",".join(deny),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr_file,
            # Not the worktree: the CLI shells out to git in its cwd before emitting
            # init, which would couple the probe to the shared control repo.
            cwd=cwd,
            # The CLI awaits a telemetry/update dial before emitting init; without
            # this it costs a hard ~15s floor whenever the network is degraded.
            # Only this var: copying opts.env would set ENABLE_TOOL_SEARCH=false and
            # silently drop ToolSearch from the registry the guard asserts on.
            env={**os.environ, "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"},
        )

        state: int | None = None
        holders: str | None = None
        try:
            assert proc.stdin is not None and proc.stdout is not None
            try:
                proc.stdin.write((user_msg + "\n").encode())
                proc.stdin.flush()
            except BrokenPipeError:
                pass  # dead child; EOF on stdout reports it and the retry absorbs it
            finally:
                with contextlib.suppress(BrokenPipeError):
                    proc.stdin.close()

            read = read_init_frame(proc, INIT_FRAME_BUDGET)
            if read.outcome is InitReadOutcome.PROCESS_EXITED:
                # Before the reap, while the pipe is still open: its inode is what
                # identifies the descendant that outlived the CLI.
                holders = _pipe_holders(proc.stdout.fileno())
            if read.frame is None:
                try:
                    # Settled before the reap: terminate() sets returncode to -15, so a
                    # read afterwards would report every budget expiry as an exit. A CLI
                    # that died before init settles within a second; a live one times out.
                    state = proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    state = None
        finally:
            # Reap so no orphan claude/node process is left running. terminate()
            # (SIGTERM) is honored promptly by node; the wait is bounded and
            # escalates to SIGKILL, so the file's collection-time timeout is
            # reachable only if the kill itself never lands.
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            if proc.stdout is not None:
                proc.stdout.close()

        stderr_file.seek(0)
        return ProbeAttempt(
            frame=read.frame,
            exit_status=state,
            stdout_bytes=read.stdout_bytes,
            stderr_text=stderr_file.read(),
            elapsed=time.monotonic() - started,
            pipe_holders=holders,
        )


def _state_text(attempt: ProbeAttempt) -> str:
    return ("process still running" if attempt.exit_status is None
            else f"process exited {attempt.exit_status}")


def _attempt_note(n: int, attempt: ProbeAttempt) -> str:
    note = (f"#{n} {_state_text(attempt)} after {attempt.elapsed:.1f}s,"
            f" {attempt.stdout_bytes} stdout bytes")
    if attempt.pipe_holders is not None:
        note += f", stdout pipe still held by: {attempt.pipe_holders}"
    return note


def _declined_silently(attempt: ProbeAttempt) -> bool:
    """Exited 0 having said nothing — the CLI never started a session. Narrower than
    "no init frame": a CLI that errored exits non-zero and one that drifted still puts
    bytes on stdout, and both of those stay loud failures."""
    return attempt.exit_status == 0 and attempt.stdout_bytes == 0


def probe_advertised_tools(cli: str, deny: list[str], cwd: Path) -> tuple[set[str], str]:
    """Spawn `cli` in imp's posture with `deny` and return the tool registry it
    advertises in the `init` system message. The init frame precedes the model
    turn, so no model round-trip occurs. Returns (advertised_tools, stderr_text).
    A spawn that yields no init frame is retried: everything the CLI does before
    init is external work a fresh spawn redoes. When no attempt yields one, skips
    if every attempt exited 0 without writing a byte — the CLI declined to start a
    session, an environment fault, and the check did NOT run — and otherwise raises
    AssertionError. Neither path returns, so a dead probe can never be mistaken for
    an empty registry."""
    t0 = time.monotonic()
    notes: list[str] = []
    attempt_stderrs: list[tuple[int, str]] = []
    attempts: list[ProbeAttempt] = []
    for n in range(1, PROBE_ATTEMPTS + 1):
        probe = _spawn_and_read_init(cli, deny, cwd)
        attempts.append(probe)
        if probe.frame is not None:
            break
        notes.append(_attempt_note(n, probe))
        attempt_stderrs.append((n, probe.stderr_text))
        if n < PROBE_ATTEMPTS:
            time.sleep(PROBE_RETRY_BACKOFF)

    last = attempts[-1]
    frame = last.frame
    if frame is None:
        total_elapsed = time.monotonic() - t0
        stderr_sections = "\n".join(
            f"CLI stderr (attempt {n}):\n{s}" for n, s in attempt_stderrs
        )
        attempts_note = ", ".join(notes)
        if all(_declined_silently(a) for a in attempts):
            pytest.skip(
                "The bundled claude CLI declined to start a session: it exited 0 without"
                f" writing a byte to stdout on every one of {PROBE_ATTEMPTS} attempts"
                f" ({total_elapsed:.1f}s total). The deny-posture check DID NOT RUN on"
                ' this host — this is an environment fault, not a "no built-ins escaped"'
                " result. Run the guard on a host where the CLI reaches its init frame.\n"
                f"CLI: {cli}\n"
                f"Attempts: {attempts_note}\n"
                f"{stderr_sections}"
            )
        raise AssertionError(
            f"No init frame from the claude CLI after {PROBE_ATTEMPTS} attempts"
            f" in {total_elapsed:.1f}s ({_state_text(last)}).\n"
            "Node startup, an rg --files scan, a ps aux, and a deferred git config precede init,\n"
            "so a stall here is the CLI's environment, not imp's deny posture.\n"
            f"Attempts: {attempts_note}\n"
            f"{stderr_sections}"
        )

    advertised: set[str] = set()
    # `tools` on init may be a list of names or of descriptors.
    tools = frame.get("tools") or frame.get("data", {}).get("tools") or []
    for t in tools:
        if isinstance(t, str):
            advertised.add(t)
        elif isinstance(t, dict) and "name" in t:
            advertised.add(t["name"])

    return advertised, last.stderr_text
