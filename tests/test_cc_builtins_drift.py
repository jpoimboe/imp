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

# A hang detector, not a tuned window: ~3.3x the worst degraded-network case and
# ~10x the worst init latency observed under a saturated parallel suite.
_INIT_FRAME_BUDGET = 60.0


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


def _probe_advertised_tools(cli: str, deny: list[str]) -> tuple[set[str], str]:
    """Spawn `cli` in imp's posture with `deny` and return the tool registry it
    advertises in the `init` system message. The init frame precedes the model
    turn, so no model round-trip occurs. Returns (advertised_tools, stderr_text)
    — stderr is the only evidence available when the probe comes back empty."""
    # Do NOT skip on CLAUDECODE=1: the CLI advertises an identical registry with
    # or without it, and such a skip would hide real drift.
    user_msg = json.dumps({
        "type": "user",
        "message": {"role": "user", "content": "say ok"},
    })

    # TemporaryFile, not PIPE: the probe blocks in readline() and never drains
    # stderr, so a PIPE could fill its OS buffer and deadlock the CLI.
    with tempfile.TemporaryFile("w+") as stderr_file:
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
            text=True,
        )

        advertised: set[str] = set()
        try:
            assert proc.stdin is not None and proc.stdout is not None
            proc.stdin.write(user_msg + "\n")
            proc.stdin.flush()
            proc.stdin.close()

            # readline() (not `for line in proc.stdout`) so the iterator's
            # read-ahead buffering never delays delivery of the init line.
            while True:
                line = proc.stdout.readline()
                if line == "":          # EOF: stdout closed before an init message
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if msg.get("type") == "system" and msg.get("subtype") == "init":
                    # `tools` on init may be a list of names or of descriptors.
                    tools = msg.get("tools") or msg.get("data", {}).get("tools") or []
                    for t in tools:
                        if isinstance(t, str):
                            advertised.add(t)
                        elif isinstance(t, dict) and "name" in t:
                            advertised.add(t["name"])
                    break
        finally:
            # Reap so no orphan claude/node process is left running. terminate()
            # (SIGTERM) is honored promptly by node; the wait() carries no numeric
            # budget, and the @pytest.mark.timeout marker is the sole backstop for
            # a genuinely hung process.
            proc.terminate()
            proc.wait()
            if proc.stdout is not None:
                proc.stdout.close()

        stderr_file.seek(0)
        stderr_text = stderr_file.read()

    return advertised, stderr_text


@pytest.mark.timeout(120)   # process-level backstop against a genuinely hung
                            # CLI (a real defect) — NOT a load budget.
def test_no_builtin_escapes_imp_deny_posture(tmp_path: Path) -> None:
    opts = ClaudeCodeAgentRunner("m")._build_options(
        _invocation(tmp_path), object(), ["bash"])
    advertised, _ = _probe_advertised_tools(_resolve_cli(opts), list(opts.disallowed_tools))

    escapees = advertised - {"ToolSearch"}
    assert not escapees, (
        f"Built-ins reachable under imp's deny posture: {sorted(escapees)}. "
        "The agent can call these from every claude_code step. Add them to "
        "ALL_CLAUDE_CODE_BUILTIN_TOOLS in _cc_builtins.py."
    )


@pytest.mark.timeout(120)   # process-level backstop against a genuinely hung
                            # CLI (a real defect) — NOT a load budget.
def test_deny_posture_probe_is_live(tmp_path: Path) -> None:
    opts = ClaudeCodeAgentRunner("m")._build_options(
        _invocation(tmp_path), object(), ["bash"])
    deny = [t for t in opts.disallowed_tools if t != "Bash"]
    advertised, stderr = _probe_advertised_tools(_resolve_cli(opts), deny)

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
