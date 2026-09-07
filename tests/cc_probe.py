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


def read_init_frame(proc: subprocess.Popen[bytes], budget: float) -> InitRead:
    """Poll `proc`'s raw stdout fd for the CLI's `init` system message, giving up
    after `budget` seconds. Bytes are buffered and split on newlines, so a frame
    delivered across two writes still parses. Non-JSON and non-init lines are
    skipped. Returns an InitRead naming which exit was taken: INIT, EOF or
    BUDGET_EXPIRED. `frame` is set exactly when the outcome is INIT."""
    assert proc.stdout is not None
    deadline = time.monotonic() + budget
    buf = b""
    stdout_bytes = 0
    with selectors.DefaultSelector() as selector:
        selector.register(proc.stdout, selectors.EVENT_READ)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                return InitRead(None, stdout_bytes, InitReadOutcome.BUDGET_EXPIRED)
            chunk = os.read(proc.stdout.fileno(), 65536)
            if not chunk:               # EOF: stdout closed before an init message
                return InitRead(None, stdout_bytes, InitReadOutcome.EOF)
            stdout_bytes += len(chunk)
            buf += chunk
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
                    return InitRead(msg, stdout_bytes, InitReadOutcome.INIT)


def _spawn_and_read_init(
    cli: str, deny: list[str], cwd: Path
) -> tuple[dict[str, Any] | None, int | None, str]:
    """One probe spawn. Returns (init frame or None, exit status or None, stderr text).
    The status is None when the CLI is still running at budget expiry."""
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
                "--max-turns", "1",
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

            frame = read_init_frame(proc, INIT_FRAME_BUDGET).frame
            if frame is None:
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
        return frame, state, stderr_file.read()


def probe_advertised_tools(cli: str, deny: list[str], cwd: Path) -> tuple[set[str], str]:
    """Spawn `cli` in imp's posture with `deny` and return the tool registry it
    advertises in the `init` system message. The init frame precedes the model
    turn, so no model round-trip occurs. Returns (advertised_tools, stderr_text).
    A spawn that yields no init frame is retried: everything the CLI does before
    init is external work a fresh spawn redoes. Raises AssertionError when no
    attempt yields an init frame, so a dead probe can never be mistaken for an
    empty registry."""
    t0 = time.monotonic()
    notes: list[str] = []
    attempt_stderrs: list[tuple[int, str]] = []
    for attempt in range(1, PROBE_ATTEMPTS + 1):
        start = time.monotonic()
        frame, state, stderr_text = _spawn_and_read_init(cli, deny, cwd)
        if frame is not None:
            break
        state_text = "process still running" if state is None else f"process exited {state}"
        notes.append(f"#{attempt} {state_text} after {time.monotonic() - start:.1f}s")
        attempt_stderrs.append((attempt, stderr_text))
        if attempt < PROBE_ATTEMPTS:
            time.sleep(PROBE_RETRY_BACKOFF)

    if frame is None:
        total_elapsed = time.monotonic() - t0
        stderr_sections = "\n".join(
            f"CLI stderr (attempt {n}):\n{s}" for n, s in attempt_stderrs
        )
        raise AssertionError(
            f"No init frame from the claude CLI after {PROBE_ATTEMPTS} attempts"
            f" in {total_elapsed:.1f}s ({state_text}).\n"
            "Node startup, an rg --files scan, a ps aux, and a deferred git config precede init,\n"
            "so a stall here is the CLI's environment, not imp's deny posture.\n"
            f"Attempts: {', '.join(notes)}\n"
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

    return advertised, stderr_text
