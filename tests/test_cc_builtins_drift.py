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
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from imp.providers.claude_code import ClaudeCodeAgentRunner
from tests import cc_probe
from tests.cc_probe import PROBE_ATTEMPTS, invocation, probe_advertised_tools, read_init_frame


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
        frame = read_init_frame(proc, 10.0)

    assert frame is not None
    assert frame["tools"] == ["Bash", "ToolSearch"]


def test_read_init_frame_honours_its_deadline_on_a_silent_process() -> None:
    with _stub_proc("import time; time.sleep(30)") as proc:
        start = time.monotonic()
        frame = read_init_frame(proc, 1.0)
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
        frame = read_init_frame(proc, 10.0)

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
