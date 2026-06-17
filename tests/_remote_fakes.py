# tests/_remote_fakes.py
"""A real, minimal fake driver process for RemoteChannel lifecycle tests.

It logs each NDJSON stdin line to --log, optionally floods --stderr-bytes to stderr
before reading stdin, and exits with code 1 when --exit 1 is given (the "fails to
come up" case). Reading stdin to EOF lets a clean teardown (imp closes stdin) end
the process by itself."""
from __future__ import annotations

import sys
from pathlib import Path

_DRIVER_SRC = '''\
import sys
import signal

args = dict(zip(sys.argv[1::2], sys.argv[2::2]))
if args.get("--exit") == "1":
    sys.exit(1)
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
