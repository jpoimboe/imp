"""Security guard: no built-in escapes imp's deny posture.

Mechanism: resolve the CLI the SDK actually spawns (its bundled binary, not
whatever `claude` is on PATH), launch it with the exact --disallowedTools
list _build_options produces, and read the `init` system message, where
claude advertises its live tool registry. Anything advertised beyond the
ToolSearch exemption is a built-in the agent could call from every
claude_code step.

Probing under the deny posture rather than bare is load-bearing: some
built-ins only materialize once others are denied, so a baseline probe
never sees them.

WebSearch / WebFetch note (§5.2):
  These two tools ARE included in ALL_CLAUDE_CODE_BUILTIN_TOOLS so that the
  drift guard still catches them if the CLI removes or renames them. However,
  they are *intentionally enabled* by _build_options when an agent declares
  web_search/web_fetch — they are NOT part of the "imp-tools-only" deny
  stance that blocks all other builtins. See
  test_web_builtins_are_intentionally_enabled_when_declared below, which pins
  that enablement so a future deny-all refactor fails loudly here.
"""

from __future__ import annotations

import contextlib
import json
import os
import selectors
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import ClaudeAgentOptions, CLINotFoundError
from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

from imp.agents.runner import AgentInvocation, Message, QuestContext, TextBlock
from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc
from imp.providers.claude_code import ClaudeCodeAgentRunner

# A hang detector, not a tuned window: ~7x the worst init latency observed under a
# saturated parallel suite, and the pre-init network dial is no longer awaited.
_INIT_FRAME_BUDGET = 30.0
# Everything the CLI does before init is external — a git call, an rg, a ps, a
# network dial — so a spawn that yields no frame is retried, stall included.
_PROBE_ATTEMPTS = 3
_PROBE_RETRY_BACKOFF = 1.0


def _invocation(tmp_path: Path, declared: frozenset[str] = frozenset()) -> AgentInvocation:
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


def _resolve_cli(opts: ClaudeAgentOptions) -> str:
    """The binary the SDK will spawn. Mirrors connect(): an explicit cli_path
    wins, else _find_cli() (bundled → PATH → fixed locations). _find_cli() alone
    ignores cli_path entirely, so both halves are required. The constructor
    spawns nothing."""
    transport = SubprocessCLITransport(prompt="", options=opts)
    try:
        return transport._cli_path or transport._find_cli()
    except CLINotFoundError:
        pytest.skip("claude CLI not found")


def _read_init_frame(proc: subprocess.Popen[bytes], budget: float) -> dict[str, Any] | None:
    """Poll `proc`'s raw stdout fd for the CLI's `init` system message, giving up
    after `budget` seconds. Bytes are buffered and split on newlines, so a frame
    delivered across two writes still parses. Non-JSON and non-init lines are
    skipped. Returns the parsed frame, or None on EOF or budget expiry."""
    assert proc.stdout is not None
    deadline = time.monotonic() + budget
    buf = b""
    with selectors.DefaultSelector() as selector:
        selector.register(proc.stdout, selectors.EVENT_READ)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                return None
            chunk = os.read(proc.stdout.fileno(), 65536)
            if not chunk:               # EOF: stdout closed before an init message
                return None
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
                    return msg


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

            frame = _read_init_frame(proc, _INIT_FRAME_BUDGET)
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
            # escalates to SIGKILL, so the @pytest.mark.timeout marker is reachable
            # only if the kill itself never lands.
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


def _probe_advertised_tools(cli: str, deny: list[str], cwd: Path) -> tuple[set[str], str]:
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
    for attempt in range(1, _PROBE_ATTEMPTS + 1):
        start = time.monotonic()
        frame, state, stderr_text = _spawn_and_read_init(cli, deny, cwd)
        if frame is not None:
            break
        state_text = "process still running" if state is None else f"process exited {state}"
        notes.append(f"#{attempt} {state_text} after {time.monotonic() - start:.1f}s")
        attempt_stderrs.append((attempt, stderr_text))
        if attempt < _PROBE_ATTEMPTS:
            time.sleep(_PROBE_RETRY_BACKOFF)

    if frame is None:
        total_elapsed = time.monotonic() - t0
        stderr_sections = "\n".join(
            f"CLI stderr (attempt {n}):\n{s}" for n, s in attempt_stderrs
        )
        raise AssertionError(
            f"No init frame from the claude CLI after {_PROBE_ATTEMPTS} attempts"
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


@pytest.mark.timeout(180)   # process-level backstop, clear of the retry loop's
                            # 3 x (30s budget + 10s reap) worst case.
def test_no_builtin_escapes_imp_deny_posture(tmp_path: Path) -> None:
    opts = ClaudeCodeAgentRunner("m")._build_options(
        _invocation(tmp_path), object(), ["bash"])
    advertised, _ = _probe_advertised_tools(
        _resolve_cli(opts), list(opts.disallowed_tools), tmp_path)

    escapees = advertised - {"ToolSearch"}
    assert not escapees, (
        f"Built-ins reachable under imp's deny posture: {sorted(escapees)}. "
        "The agent can call these from every claude_code step. Add them to "
        "ALL_CLAUDE_CODE_BUILTIN_TOOLS in _cc_builtins.py."
    )


@pytest.mark.timeout(180)   # process-level backstop, clear of the retry loop's
                            # 3 x (30s budget + 10s reap) worst case.
def test_deny_posture_probe_is_live(tmp_path: Path) -> None:
    opts = ClaudeCodeAgentRunner("m")._build_options(
        _invocation(tmp_path), object(), ["bash"])
    deny = [t for t in opts.disallowed_tools if t != "Bash"]
    advertised, stderr = _probe_advertised_tools(_resolve_cli(opts), deny, tmp_path)

    assert "Bash" in advertised, (
        "Probe is inert: expected Bash to be advertised when only ToolSearch "
        f"and Bash are exempt, got {sorted(advertised)}. The probe no longer "
        "reflects the CLI's init output — fix it before trusting the "
        "deny-posture guard."
        f"\nCLI stderr:\n{stderr}"
    )


def test_web_builtins_are_intentionally_enabled_when_declared(tmp_path: Path) -> None:
    """WebSearch/WebFetch are deliberately exempted from the deny list and
    allowlisted when an agent declares web_search/web_fetch (spec §5.2). This
    pins that intent so a future deny-all refactor fails here."""
    inv = _invocation(tmp_path, frozenset({"web_search", "web_fetch"}))
    opts = ClaudeCodeAgentRunner("m")._build_options(inv, object(), ["bash"])
    assert {"WebSearch", "WebFetch"} <= set(opts.allowed_tools)
    assert not ({"WebSearch", "WebFetch"} & set(opts.disallowed_tools))


_STUB_INIT_FRAME = '{"type": "system", "subtype": "init", "tools": ["Bash", "ToolSearch"]}'


@contextlib.contextmanager
def _stub_proc(code: str) -> Iterator[subprocess.Popen[bytes]]:
    """A short-lived python stub on binary pipes, shaped like the CLI probe's."""
    proc = subprocess.Popen(
        [sys.executable, "-c", code],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
    )
    try:
        yield proc
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        assert proc.stdout is not None
        proc.stdout.close()


def test_read_init_frame_returns_the_init_frame() -> None:
    code = (
        "import sys, time\n"
        "sys.stdout.write('{\"type\": \"system\", \"subtype\": \"compact\"}\\n')\n"
        "sys.stdout.write('not json at all\\n')\n"
        f"sys.stdout.write({_STUB_INIT_FRAME!r} + '\\n')\n"
        "sys.stdout.flush()\n"
        "time.sleep(30)\n"
    )
    with _stub_proc(code) as proc:
        frame = _read_init_frame(proc, 10.0)

    assert frame is not None
    assert frame["tools"] == ["Bash", "ToolSearch"]


def test_read_init_frame_honours_its_deadline_on_a_silent_process() -> None:
    with _stub_proc("import time; time.sleep(30)") as proc:
        start = time.monotonic()
        frame = _read_init_frame(proc, 1.0)
        elapsed = time.monotonic() - start

    assert frame is None
    assert elapsed < 10.0, f"reader blocked past its 1s budget ({elapsed:.1f}s)"


def test_read_init_frame_parses_a_frame_split_across_writes() -> None:
    head, tail = _STUB_INIT_FRAME[:20], _STUB_INIT_FRAME[20:]
    code = (
        "import sys, time\n"
        f"sys.stdout.write({head!r})\n"
        "sys.stdout.flush()\n"
        "time.sleep(0.2)\n"
        f"sys.stdout.write({tail!r} + '\\n')\n"
        "sys.stdout.flush()\n"
        "time.sleep(30)\n"
    )
    with _stub_proc(code) as proc:
        frame = _read_init_frame(proc, 10.0)

    assert frame is not None
    assert frame["tools"] == ["Bash", "ToolSearch"]


def test_probe_raises_when_no_init_frame_arrives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The vacuous pass, pinned closed at the function that used to permit it: a
    probe that dies must raise, not report an empty registry with no escapees."""
    stub = tmp_path / "silent-cli"
    stub.write_text("#!/bin/sh\necho stub-cli-stderr >&2\nexec sleep 30\n")
    stub.chmod(0o755)
    # The stub's stderr need only land before terminate() — a budget + 1s settle
    # after spawn — not inside the budget window: it is read after the reap.
    monkeypatch.setattr(sys.modules[__name__], "_INIT_FRAME_BUDGET", 1.5)
    monkeypatch.setattr(sys.modules[__name__], "_PROBE_ATTEMPTS", 2)
    monkeypatch.setattr(sys.modules[__name__], "_PROBE_RETRY_BACKOFF", 0.0)

    with pytest.raises(AssertionError) as excinfo:
        _probe_advertised_tools(str(stub), ["Bash"], tmp_path)

    message = str(excinfo.value)
    assert "No init frame from the claude CLI after 2 attempts" in message
    assert "(process still running)." in message
    assert "stub-cli-stderr" in message
    attempts_line = next(line for line in message.splitlines() if line.startswith("Attempts:"))
    assert attempts_line.startswith("Attempts: #1 process still running after ")
    assert "#2 process still running after " in attempts_line, (
        "a stall is external work a fresh spawn redoes — it must be retried")


def test_probe_retries_when_the_cli_dies_before_init(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A CLI that dies before init is a transient environmental fault, so the probe
    respawns — and still raises, never reporting an empty registry, if every try dies."""
    spawns = tmp_path / "spawns"
    stub = tmp_path / "dying-cli"
    stub.write_text(
        "#!/bin/sh\n"
        f"echo x >> {spawns}\n"
        "echo died-early-stderr >&2\n"
        "exit 1\n"
    )
    stub.chmod(0o755)
    monkeypatch.setattr(sys.modules[__name__], "_PROBE_RETRY_BACKOFF", 0.0)

    with pytest.raises(AssertionError) as excinfo:
        _probe_advertised_tools(str(stub), ["Bash"], tmp_path)

    message = str(excinfo.value)
    assert "process exited 1" in message
    assert "died-early-stderr" in message
    assert "CLI stderr (attempt 1):" in message
    assert f"CLI stderr (attempt {_PROBE_ATTEMPTS}):" in message
    assert len(spawns.read_text().splitlines()) == _PROBE_ATTEMPTS
