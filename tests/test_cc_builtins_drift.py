"""Security guard: no built-in escapes imp's deny posture — the CLI-free half.

The probes that spawn the bundled CLI are in
tests/test_cc_builtins_drift_live.py behind the `live` marker; their shared
machinery is tests/cc_probe.py. What stays here needs no CLI, so the
no-green-from-a-dead-probe property is checked on every fast-gate lap.

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
import os
import re
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from imp.providers.claude_code import ClaudeCodeAgentRunner
from tests import cc_probe
from tests.cc_probe import (
    PROBE_ATTEMPTS,
    InitReadOutcome,
    invocation,
    probe_advertised_tools,
    read_init_frame,
)


def test_web_builtins_are_intentionally_enabled_when_declared(tmp_path: Path) -> None:
    """WebSearch/WebFetch are deliberately exempted from the deny list and
    allowlisted when an agent declares web_search/web_fetch (spec §5.2). This
    pins that intent so a future deny-all refactor fails here."""
    inv = invocation(tmp_path, frozenset({"web_search", "web_fetch"}))
    opts = ClaudeCodeAgentRunner("m")._build_options(inv, object(), ["bash"])
    assert {"WebSearch", "WebFetch"} <= set(opts.allowed_tools)
    assert not ({"WebSearch", "WebFetch"} & set(opts.disallowed_tools))


_STUB_INIT_FRAME = '{"type": "system", "subtype": "init", "tools": ["Bash", "ToolSearch"]}'


@contextlib.contextmanager
def _stub_proc(argv: list[str]) -> Iterator[subprocess.Popen[bytes]]:
    """A short-lived stub on binary pipes, shaped like the CLI probe's."""
    proc = subprocess.Popen(
        argv,
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


def _sh_stub(path: Path, script: str) -> Path:
    """An executable /bin/sh stub at `path`."""
    path.write_text(script)
    path.chmod(0o755)
    return path


def test_read_init_frame_returns_the_init_frame() -> None:
    code = (
        "import sys, time\n"
        "sys.stdout.write('{\"type\": \"system\", \"subtype\": \"compact\"}\\n')\n"
        "sys.stdout.write('not json at all\\n')\n"
        f"sys.stdout.write({_STUB_INIT_FRAME!r} + '\\n')\n"
        "sys.stdout.flush()\n"
        "time.sleep(30)\n"
    )
    with _stub_proc([sys.executable, "-c", code]) as proc:
        read = read_init_frame(proc, 10.0)

    assert read.outcome is InitReadOutcome.INIT
    assert read.stdout_bytes > 0
    assert read.frame is not None
    assert read.frame["tools"] == ["Bash", "ToolSearch"]


def test_read_init_frame_honours_its_deadline_on_a_silent_process() -> None:
    with _stub_proc([sys.executable, "-c", "import time; time.sleep(30)"]) as proc:
        start = time.monotonic()
        read = read_init_frame(proc, 1.0)
        elapsed = time.monotonic() - start

    assert read.outcome is InitReadOutcome.BUDGET_EXPIRED
    assert read.frame is None
    assert read.stdout_bytes == 0
    assert elapsed < 10.0, f"reader blocked past its 1s budget ({elapsed:.1f}s)"


def test_read_init_frame_reports_eof_when_stdout_closes_first() -> None:
    with _stub_proc([sys.executable, "-c", "pass"]) as proc:
        read = read_init_frame(proc, 10.0)

    assert read.outcome is InitReadOutcome.EOF
    assert read.frame is None
    assert read.stdout_bytes == 0


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
    with _stub_proc([sys.executable, "-c", code]) as proc:
        read = read_init_frame(proc, 10.0)

    assert read.outcome is InitReadOutcome.INIT
    assert read.stdout_bytes > 0
    assert read.frame is not None
    assert read.frame["tools"] == ["Bash", "ToolSearch"]


def test_read_init_frame_gives_up_when_the_cli_exits_holding_the_pipe(tmp_path: Path) -> None:
    """The reported 93s signature: the CLI exits 0 but a descendant inherited stdout, so
    the pipe never reaches EOF. The reader must key on the process, not the pipe."""
    stub = _sh_stub(tmp_path / "backgrounding-cli", "#!/bin/sh\n(sleep 30) &\nexit 0\n")

    with _stub_proc([str(stub)]) as proc:
        start = time.monotonic()
        read = read_init_frame(proc, 30.0)
        elapsed = time.monotonic() - start

    assert read.outcome is InitReadOutcome.PROCESS_EXITED
    assert read.frame is None
    assert read.stdout_bytes == 0
    assert elapsed < 15.0, f"reader waited on pipe EOF, not on the process ({elapsed:.1f}s)"


def test_read_init_frame_counts_bytes_written_before_a_held_pipe_exit(tmp_path: Path) -> None:
    """Bytes that landed before the exit are accounted for, never dropped into an apparent
    silence — a CLI that spoke must stay a loud failure, not a silent-exit skip."""
    stub = _sh_stub(
        tmp_path / "chatty-backgrounding-cli",
        "#!/bin/sh\n(sleep 30) &\n"
        "echo '{\"type\": \"system\", \"subtype\": \"compact\"}'\n"
        "exit 0\n",
    )

    with _stub_proc([str(stub)]) as proc:
        read = read_init_frame(proc, 30.0)

    assert read.outcome is InitReadOutcome.PROCESS_EXITED
    assert read.frame is None
    assert read.stdout_bytes > 0


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
    monkeypatch.setattr(cc_probe, "INIT_FRAME_BUDGET", 1.5)
    monkeypatch.setattr(cc_probe, "PROBE_ATTEMPTS", 2)
    monkeypatch.setattr(cc_probe, "PROBE_RETRY_BACKOFF", 0.0)

    with pytest.raises(AssertionError) as excinfo:
        probe_advertised_tools(str(stub), ["Bash"], tmp_path)

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
    monkeypatch.setattr(cc_probe, "PROBE_RETRY_BACKOFF", 0.0)

    with pytest.raises(AssertionError) as excinfo:
        probe_advertised_tools(str(stub), ["Bash"], tmp_path)

    message = str(excinfo.value)
    assert "process exited 1" in message
    assert "died-early-stderr" in message
    assert "CLI stderr (attempt 1):" in message
    assert f"CLI stderr (attempt {PROBE_ATTEMPTS}):" in message
    assert len(spawns.read_text().splitlines()) == PROBE_ATTEMPTS


def test_spawn_attempt_names_the_process_holding_the_stdout_pipe(tmp_path: Path) -> None:
    """The evidence missing from the original report: the CLI exited yet a descendant kept
    our stdout pipe open, so the attempt records who holds it — never the prober itself."""
    stub = _sh_stub(tmp_path / "backgrounding-cli", "#!/bin/sh\n(sleep 30) &\nexit 0\n")

    attempt = cc_probe._spawn_and_read_init(str(stub), ["Bash"], tmp_path)

    assert attempt.frame is None
    assert attempt.exit_status == 0
    assert attempt.stdout_bytes == 0
    assert attempt.elapsed < 15.0
    assert attempt.pipe_holders is not None
    assert f"pid {os.getpid()} (" not in attempt.pipe_holders
    if Path("/proc/self/fd").is_dir():
        assert re.fullmatch(
            r"pid \d+ \([^()]*\)(, pid \d+ \([^()]*\))*", attempt.pipe_holders
        ), attempt.pipe_holders
    else:
        assert attempt.pipe_holders == "unknown"


def test_spawn_attempt_has_no_pipe_holders_when_stdout_reaches_eof(tmp_path: Path) -> None:
    """A plain exit whose pipe closes with it is not anomalous: no scan, no clause."""
    stub = _sh_stub(tmp_path / "quiet-cli", "#!/bin/sh\nexit 0\n")

    attempt = cc_probe._spawn_and_read_init(str(stub), ["Bash"], tmp_path)

    assert attempt.pipe_holders is None
    assert attempt.frame is None
    assert attempt.exit_status == 0
    assert attempt.stdout_bytes == 0
    assert attempt.elapsed < 15.0


def test_probe_skips_when_the_cli_exits_zero_in_silence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reported environment fault: a CLI that exits 0 having written nothing has
    declined to start a session, so the guard reports that it did not run — never a pass."""
    spawns = tmp_path / "spawns"
    stub = _sh_stub(
        tmp_path / "declining-cli",
        f"#!/bin/sh\necho x >> {spawns}\nexit 0\n",
    )
    monkeypatch.setattr(cc_probe, "PROBE_RETRY_BACKOFF", 0.0)

    with pytest.raises(pytest.skip.Exception) as excinfo:
        probe_advertised_tools(str(stub), ["Bash"], tmp_path)

    reason = str(excinfo.value)
    assert "The bundled claude CLI declined to start a session" in reason
    assert "DID NOT RUN" in reason
    assert "process exited 0" in reason
    assert "0 stdout bytes" in reason
    assert str(stub) in reason, "the skip must name the CLI it could not exercise"
    # This stub's pipe closes with it: nothing anomalous, so no holder clause (AC 9b).
    assert "stdout pipe still held by" not in reason
    assert len(spawns.read_text().splitlines()) == PROBE_ATTEMPTS, (
        "the silent exit is still retried before it is believed")


def test_probe_raises_when_the_cli_speaks_but_never_inits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drift, not a declined session: a CLI that emitted frames without init has changed
    or broken, so it stays a loud failure — only total silence earns the skip."""
    stub = _sh_stub(
        tmp_path / "chatty-cli",
        "#!/bin/sh\n"
        "echo '{\"type\": \"system\", \"subtype\": \"compact\"}'\n"
        "exit 0\n",
    )
    monkeypatch.setattr(cc_probe, "PROBE_RETRY_BACKOFF", 0.0)

    with pytest.raises(AssertionError) as excinfo:
        probe_advertised_tools(str(stub), ["Bash"], tmp_path)

    message = str(excinfo.value)
    assert "No init frame from the claude CLI" in message
    assert "declined to start a session" not in message
    written = re.search(r"#1 process exited 0 after [\d.]+s, (\d+) stdout bytes", message)
    assert written is not None, message
    assert int(written.group(1)) > 0, "bytes on stdout must be visible in the evidence"
