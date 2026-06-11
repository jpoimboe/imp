# tests/_remote_fakes.py
"""A real, minimal fake driver process for RemoteChannel lifecycle tests.

It logs each NDJSON stdin line to --log, optionally emits --reply on stdout once
at startup (so next_reply has something to read), and exits with code 1 when
--exit 1 is given (the "fails to come up" case). Reading stdin to EOF lets a
clean teardown (imp closes stdin) end the process by itself."""
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
reply = args.get("--reply")
if reply:
    sys.stdout.write(reply + "\\n")
    sys.stdout.flush()
for line in sys.stdin:
    if log:
        with open(log, "a") as f:
            f.write(line)
'''


def fake_driver_command(
    tmp_path: Path, *, log: Path | None = None, reply: str | None = None,
    exit_immediately: bool = False, ignore_sigterm: bool = False,
) -> list[str]:
    script = tmp_path / "fake_driver.py"
    script.write_text(_DRIVER_SRC)
    cmd = [sys.executable, str(script)]
    if log is not None:
        cmd += ["--log", str(log)]
    if reply is not None:
        cmd += ["--reply", reply]
    if exit_immediately:
        cmd += ["--exit", "1"]
    if ignore_sigterm:
        cmd += ["--ignore-sigterm", "1"]
    return cmd
