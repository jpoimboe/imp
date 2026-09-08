"""Unit tests for imp's own `codex exec --json` driver: argv, env, spawn, read loop,
stderr drain, and group-reaping teardown."""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from imp.providers.codex_exec import (
    CODEX_STREAM_LIMIT,
    STDERR_ERROR_CHARS,
    Codex,
    CodexExecError,
    CodexOptions,
    ThreadOptions,
    _drain_stderr,
    _run_exec,
    build_command_args,
)
from imp.providers.errors import ConfigError
from imp.providers.stderr_sink import StderrSink, current_stderr_sink
from tests.conftest import _pid_gone_or_zombie


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


_OPTS_HIGH = ThreadOptions(
    model="gpt-5-codex", sandbox_mode="read-only", approval_policy="never",
    network_access_enabled=False, model_reasoning_effort="high")
_OPTS_LOW = ThreadOptions(
    model="gpt-5-codex", sandbox_mode="read-only", approval_policy="never",
    network_access_enabled=False)


def test_medium_tier_argv_is_exactly_the_production_flag_list() -> None:
    assert build_command_args(_OPTS_HIGH, None) == [
        "exec", "--json", "--model", "gpt-5-codex", "--sandbox", "read-only",
        "--config", 'model_reasoning_effort="high"',
        "--config", "sandbox_workspace_write.network_access=false",
        "--config", 'approval_policy="never"']


def test_an_unset_reasoning_effort_drops_only_that_config_pair() -> None:
    args = build_command_args(_OPTS_LOW, None)
    assert not any("model_reasoning_effort" in a for a in args)
    assert args == [
        "exec", "--json", "--model", "gpt-5-codex", "--sandbox", "read-only",
        "--config", "sandbox_workspace_write.network_access=false",
        "--config", 'approval_policy="never"']


def test_network_access_renders_as_a_lowercase_json_bool() -> None:
    enabled = ThreadOptions(
        model="m", sandbox_mode="workspace-write", approval_policy="never",
        network_access_enabled=True)
    assert "sandbox_workspace_write.network_access=true" in build_command_args(enabled, None)
    assert "sandbox_workspace_write.network_access=false" in build_command_args(_OPTS_LOW, None)


def test_resuming_appends_the_thread_id_as_the_final_two_arguments() -> None:
    args = build_command_args(_OPTS_HIGH, "thr-42")
    assert args[-2:] == ["resume", "thr-42"]
    assert args[:-2] == build_command_args(_OPTS_HIGH, None)


def test_argv_carries_the_current_json_flag_and_no_web_search_option() -> None:
    args = build_command_args(_OPTS_HIGH, "thr-42")
    assert "--json" in args
    assert "--experimental-json" not in args
    assert not any("features.web_search_request" in a for a in args)


# --- Fake codex children ----------------------------------------------------

def _fake_codex(tmp_path: Path, body: str, name: str = "codex") -> Path:
    script = tmp_path / name
    script.write_text(f"#!{sys.executable}\n{body}")
    script.chmod(0o755)
    return script


_BIG_LINE = 200 * 1024

_ECHO_STDIN = """\
import sys
prompt = sys.stdin.read()
sys.stdout.write(prompt.replace("\\n", " ") + "\\n")
sys.stdout.write(repr(sys.argv[1:]) + "\\n")
sys.stdout.flush()
"""

_BIG_WRITER = f"""\
import sys
sys.stdin.read()
sys.stdout.write("x" * {_BIG_LINE} + "\\n")
sys.stdout.flush()
"""

_CHUNKED_BIG_WRITER = f"""\
import sys, time
sys.stdin.read()
data = "x" * {_BIG_LINE} + "\\n"
for i in range(0, len(data), 4096):
    sys.stdout.write(data[i:i + 4096])
    sys.stdout.flush()
    time.sleep(0.0005)
"""

_PGID_REPORTER = """\
import os, sys
sys.stdin.read()
sys.stdout.write(f"{os.getpid()} {os.getpgid(0)}\\n")
sys.stdout.flush()
"""

_CHATTY_STDERR = """\
import sys
sys.stdin.read()
sys.stderr.write("e" * (1024 * 1024))
sys.stderr.flush()
sys.stdout.write("done\\n")
sys.stdout.flush()
"""

_FAILING = """\
import sys
sys.stdin.read()
sys.stderr.write("boom: codex could not start\\n")
sys.stderr.flush()
sys.exit(3)
"""

_FAILING_CHATTY = """\
import sys
sys.stdin.read()
sys.stderr.write("e" * (1024 * 1024))
sys.stderr.write("\\nboom: the real error\\n")
sys.stderr.flush()
sys.exit(3)
"""

_GRANDPARENT = """\
import os, subprocess, sys, time
sys.stdin.read()
kid = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
sys.stdout.write(f"{os.getpid()} {kid.pid}\\n")
sys.stdout.flush()
time.sleep(300)
"""


_ESCAPEE = """\
import os, subprocess, sys, time
sys.stdin.read()
kid = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"],
                       start_new_session=True)
sys.stdout.write(f"{os.getpid()} {kid.pid}\\n")
sys.stdout.flush()
time.sleep(300)
"""


def _await_death(pid: int, complaint: str) -> None:
    deadline = time.monotonic() + 20.0  # generous; only burned on the failure path
    while not _pid_gone_or_zombie(pid):
        if time.monotonic() > deadline:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
            pytest.fail(complaint)
        time.sleep(0.05)


async def _collect(exe: Path, prompt: str, args: list[str] | None = None) -> list[str]:
    lines: list[str] = []
    gen = _run_exec(str(exe), args or [], dict(os.environ), prompt)
    async with contextlib.aclosing(gen):
        async for line in gen:
            lines.append(line)
    return lines


# --- The read loop ----------------------------------------------------------

@pytest.mark.anyio
async def test_the_prompt_reaches_the_child_on_stdin_and_never_in_argv(tmp_path: Path) -> None:
    exe = _fake_codex(tmp_path, _ECHO_STDIN)
    lines = await _collect(exe, "solve the thing", ["exec", "--json"])
    assert lines[0] == "solve the thing"
    assert lines[1] == repr(["exec", "--json"])


@pytest.mark.anyio
async def test_a_jsonl_line_over_64_kib_is_read_whole(tmp_path: Path) -> None:
    """asyncio's 64 KiB default would raise ValueError('Separator is found, but ...')."""
    exe = _fake_codex(tmp_path, _BIG_WRITER)
    lines = await _collect(exe, "go")
    assert len(lines[0]) == _BIG_LINE
    assert CODEX_STREAM_LIMIT == 64 * 1024 * 1024


@pytest.mark.anyio
async def test_a_chunked_jsonl_line_over_64_kib_is_read_whole(tmp_path: Path) -> None:
    """Same line in spaced 4 KiB writes: the granularity a loaded machine produces,
    which pauses the reader early enough to block the child mid-write."""
    exe = _fake_codex(tmp_path, _CHUNKED_BIG_WRITER)
    lines = await _collect(exe, "go")
    assert len(lines[0]) == _BIG_LINE


@pytest.mark.anyio
async def test_the_child_is_its_own_session_leader(tmp_path: Path) -> None:
    exe = _fake_codex(tmp_path, _PGID_REPORTER)
    lines = await _collect(exe, "go")
    pid, pgid = (int(x) for x in lines[0].split())
    assert pid == pgid
    assert pgid != os.getpgid(0)


@pytest.mark.anyio
async def test_a_child_flooding_stderr_completes_instead_of_deadlocking(
        tmp_path: Path) -> None:
    exe = _fake_codex(tmp_path, _CHATTY_STDERR)
    assert await _collect(exe, "go") == ["done"]


@pytest.mark.anyio
async def test_a_non_zero_exit_raises_carrying_the_child_stderr(tmp_path: Path) -> None:
    exe = _fake_codex(tmp_path, _FAILING)
    with pytest.raises(CodexExecError) as excinfo:
        await _collect(exe, "go")
    assert "boom: codex could not start" in str(excinfo.value)
    assert "3" in str(excinfo.value)


@pytest.mark.anyio
async def test_a_flooding_child_produces_a_bounded_error_message_with_the_tail(
        tmp_path: Path) -> None:
    exe = _fake_codex(tmp_path, _FAILING_CHATTY)
    with pytest.raises(CodexExecError) as excinfo:
        await _collect(exe, "go")
    msg = str(excinfo.value)
    assert "boom: the real error" in msg
    assert "[stderr truncated]" in msg
    assert len(msg) < 2 * STDERR_ERROR_CHARS


@pytest.mark.anyio
async def test_closing_the_line_stream_kills_the_child_and_its_grandchild(
        tmp_path: Path) -> None:
    exe = _fake_codex(tmp_path, _GRANDPARENT)
    gen = _run_exec(str(exe), [], dict(os.environ), "go")
    async with contextlib.aclosing(gen):
        async for line in gen:
            child, grandchild = (int(x) for x in line.split())
            break
    _await_death(child, f"codex child {child} survived teardown")
    _await_death(grandchild, f"codex grandchild {grandchild} survived teardown")


@pytest.mark.anyio
async def test_teardown_returns_when_a_descendant_escapes_the_group(tmp_path: Path) -> None:
    """Process.wait() returns only once the child's pipes disconnect, so an escapee
    still holding them would hang teardown forever without the bounded reap."""
    exe = _fake_codex(tmp_path, _ESCAPEE)
    gen = _run_exec(str(exe), [], dict(os.environ), "go")
    async with contextlib.aclosing(gen):
        async for line in gen:
            child, escapee = (int(x) for x in line.split())
            break
    try:
        _await_death(child, f"codex child {child} survived teardown")
        assert not _pid_gone_or_zombie(escapee)  # it left the group by design
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(escapee, signal.SIGKILL)


# --- stderr tee -------------------------------------------------------------

_STDERR_THEN_BLOCK = """\
import sys, time
sys.stdin.read()
sys.stderr.write("warmup: talking to the model\\n")
sys.stderr.flush()
sys.stdout.write("first line\\n")
sys.stdout.flush()
time.sleep(300)
"""


class _ChunkedStream:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    async def read(self, n: int = -1) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""


@pytest.mark.anyio
async def test_a_multibyte_char_split_across_reads_survives_the_tee(
        tmp_path: Path) -> None:
    sink = StderrSink(tmp_path / "stderr.log")
    text = await _drain_stderr(_ChunkedStream([b"caf\xc3", b"\xa9\n"]), sink)  # type: ignore[arg-type]
    assert text == "café\n"
    assert sink.tail() == "café\n"
    assert "�" not in (sink.tail() or "")


@pytest.mark.anyio
async def test_child_stderr_reaches_the_active_sink(tmp_path: Path) -> None:
    exe = _fake_codex(tmp_path, _FAILING)
    sink = StderrSink(tmp_path / "stderr.log")
    token = current_stderr_sink.set(sink)
    try:
        with pytest.raises(CodexExecError):
            await _collect(exe, "go")
    finally:
        current_stderr_sink.reset(token)
    assert sink.tail() == "boom: codex could not start\n"


@pytest.mark.anyio
async def test_stderr_reaches_the_sink_while_the_child_is_still_running(
        tmp_path: Path) -> None:
    """The drain runs from spawn, not after the stdout loop: a child that writes
    stderr and then blocks holding both streams open still feeds the sink."""
    exe = _fake_codex(tmp_path, _STDERR_THEN_BLOCK)
    sink = StderrSink(tmp_path / "stderr.log")
    token = current_stderr_sink.set(sink)
    try:
        gen = _run_exec(str(exe), [], dict(os.environ), "go")
        async with contextlib.aclosing(gen):
            async for line in gen:
                assert line == "first line"
                break
            deadline = time.monotonic() + 20.0
            while "warmup: talking to the model" not in (sink.tail() or ""):
                if time.monotonic() > deadline:
                    pytest.fail("stderr never reached the sink while the child ran")
                await asyncio.sleep(0.05)
    finally:
        current_stderr_sink.reset(token)


# --- The client and a streamed turn -----------------------------------------

_JSONL_CHILD = """\
import json, os, sys
prompt = sys.stdin.read()
print(json.dumps({"type": "thread.started", "thread_id": "thr-9"}), flush=True)
print(json.dumps({"type": "item.completed", "item": {
    "id": "a1", "type": "agent_message",
    "text": json.dumps({"prompt": prompt, "argv": sys.argv[1:],
                        "env": dict(os.environ)})}}), flush=True)
"""


def _client(tmp_path: Path, body: str, monkeypatch: pytest.MonkeyPatch,
            env: dict[str, str] | None = None) -> Codex:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    _fake_codex(bindir, body)
    monkeypatch.setenv("PATH", str(bindir))
    return Codex(CodexOptions(env=env if env is not None else dict(os.environ)))


async def _first_report(thread: Any, prompt: str) -> dict[str, Any]:
    turn = await thread.run_streamed(prompt)
    async with contextlib.aclosing(turn.events) as events:
        async for event in events:
            if event.type == "item.completed":
                return json.loads(event.item.text)  # type: ignore[no-any-return]
    raise AssertionError("child produced no item.completed event")


def test_a_missing_codex_binary_fails_client_construction(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    with pytest.raises(ConfigError) as excinfo:
        Codex(CodexOptions(env={}))
    assert str(excinfo.value) == "codex CLI not found on PATH"


@pytest.mark.anyio
async def test_a_turn_streams_parsed_events_and_keeps_the_prompt_out_of_argv(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(tmp_path, _JSONL_CHILD, monkeypatch)
    thread = client.start_thread(_OPTS_HIGH)
    report = await _first_report(thread, "do the thing")
    assert report["prompt"] == "do the thing"
    assert not any("do the thing" in a for a in report["argv"])
    assert report["argv"] == build_command_args(_OPTS_HIGH, None)


@pytest.mark.anyio
async def test_the_supplied_env_reaches_the_child_with_the_originator_added(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    supplied = {**os.environ, "CODEX_HOME": "/tmp/codex-home", "IMP_MCP_TOKEN": "tok-1"}
    client = _client(tmp_path, _JSONL_CHILD, monkeypatch, env=supplied)
    report = await _first_report(client.start_thread(_OPTS_HIGH), "go")
    assert report["env"]["CODEX_HOME"] == "/tmp/codex-home"
    assert report["env"]["IMP_MCP_TOKEN"] == "tok-1"
    assert report["env"]["CODEX_INTERNAL_ORIGINATOR_OVERRIDE"] == "codex_sdk_py"


@pytest.mark.anyio
async def test_an_explicit_originator_is_left_alone(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    supplied = {**os.environ, "CODEX_INTERNAL_ORIGINATOR_OVERRIDE": "mine"}
    client = _client(tmp_path, _JSONL_CHILD, monkeypatch, env=supplied)
    report = await _first_report(client.start_thread(_OPTS_HIGH), "go")
    assert report["env"]["CODEX_INTERNAL_ORIGINATOR_OVERRIDE"] == "mine"


_JSONL_GRANDPARENT = """\
import json, os, subprocess, sys, time
sys.stdin.read()
kid = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
print(json.dumps({"type": "item.completed", "item": {
    "id": "a1", "type": "agent_message", "text": f"{os.getpid()} {kid.pid}"}}), flush=True)
time.sleep(300)
"""


@pytest.mark.anyio
async def test_the_second_turn_resumes_the_thread_started_id(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(tmp_path, _JSONL_CHILD, monkeypatch)
    thread = client.start_thread(_OPTS_HIGH)
    first = await _first_report(thread, "one")
    assert first["argv"] == build_command_args(_OPTS_HIGH, None)
    second = await _first_report(thread, "two")
    assert second["argv"][-2:] == ["resume", "thr-9"]


@pytest.mark.anyio
async def test_a_resumed_thread_sends_resume_on_its_first_turn(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(tmp_path, _JSONL_CHILD, monkeypatch)
    report = await _first_report(client.resume_thread("thr-old", _OPTS_HIGH), "go")
    assert report["argv"][-2:] == ["resume", "thr-old"]


@pytest.mark.anyio
async def test_closing_the_event_stream_reaps_the_whole_child_group(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(tmp_path, _JSONL_GRANDPARENT, monkeypatch)
    turn = await client.start_thread(_OPTS_HIGH).run_streamed("go")
    async with contextlib.aclosing(turn.events) as events:
        async for event in events:
            child, grandchild = (int(x) for x in event.item.text.split())  # type: ignore[union-attr]
            break
    _await_death(child, f"codex child {child} survived aclose()")
    _await_death(grandchild, f"codex grandchild {grandchild} survived aclose()")


@pytest.mark.anyio
async def test_cancelling_the_consumer_reaps_the_whole_child_group(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(tmp_path, _JSONL_GRANDPARENT, monkeypatch)
    turn = await client.start_thread(_OPTS_HIGH).run_streamed("go")
    seen: list[Any] = []

    async def _consume() -> None:
        async for event in turn.events:
            seen.append(event)

    task = asyncio.create_task(_consume())
    deadline = time.monotonic() + 20.0
    while not seen:
        if time.monotonic() > deadline:
            task.cancel()
            pytest.fail("fake codex child never emitted its pids")
        await asyncio.sleep(0.05)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    child, grandchild = (int(x) for x in seen[0].item.text.split())
    _await_death(child, f"codex child {child} survived cancellation")
    _await_death(grandchild, f"codex grandchild {grandchild} survived cancellation")
