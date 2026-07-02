# tests/_remote_fakes.py
"""A real, minimal fake driver process for RemoteChannel lifecycle tests.

It logs each NDJSON stdin line to --log and optionally floods --stderr-bytes to
stderr before reading stdin. Reading stdin to EOF lets a clean teardown (imp closes
stdin) end the process by itself. The "fails to come up" case (exit_immediately) is
a separate near-instant shell command — see fake_driver_command."""
from __future__ import annotations

import sys
from pathlib import Path

_DRIVER_SRC = '''\
import sys
import signal

args = dict(zip(sys.argv[1::2], sys.argv[2::2]))
if args.get("--ignore-sigterm") == "1":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
log = args.get("--log")
stderr_bytes = args.get("--stderr-bytes")
if stderr_bytes:
    # Flood stderr BEFORE reading stdin: past a pipe buffer (~64KiB) this blocks
    # unless imp captures *and* drains the driver's stderr.
    sys.stderr.write("x" * int(stderr_bytes) + "\\n")
    sys.stderr.flush()
for line in sys.stdin:
    if log:
        with open(log, "a") as f:
            f.write(line)
'''


def fake_driver_command(
    tmp_path: Path, *, log: Path | None = None,
    exit_immediately: bool = False, ignore_sigterm: bool = False,
    stderr_bytes: int | None = None,
) -> list[str]:
    if exit_immediately:
        # The "fails to come up" case: exit 1 without paying Python-interpreter
        # startup. On a loaded box the child's boot can exceed RemoteChannel's
        # crash-probe window (_CRASH_PROBE_SECONDS, 0.2s), so a Python driver that
        # exits on startup can still look alive at the probe's single poll —
        # flaking the "start returns False" assertion. A shell exit is near-instant
        # and reliably lands inside the window. This mode is never combined with
        # the other flags below (they configure a driver that stays up).
        return ["/bin/sh", "-c", "exit 1"]
    script = tmp_path / "fake_driver.py"
    script.write_text(_DRIVER_SRC)
    cmd = [sys.executable, str(script)]
    if log is not None:
        cmd += ["--log", str(log)]
    if exit_immediately:
        cmd += ["--exit", "1"]
    if ignore_sigterm:
        cmd += ["--ignore-sigterm", "1"]
    if stderr_bytes is not None:
        cmd += ["--stderr-bytes", str(stderr_bytes)]
    return cmd
