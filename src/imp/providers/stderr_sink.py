# src/imp/providers/stderr_sink.py
"""Bounded capture of a provider CLI subprocess's stderr.

Each provider builds one StderrSink per step from `invocation.transcript_dir`,
feeds the subprocess's stderr into it, and folds `tail()` into the error-event
detail on failure. The full stream lands in `<step>/stderr.log` (capped); the
in-memory tail survives a disk-write failure so the error detail is never lost.
"""
from __future__ import annotations

import contextvars
from pathlib import Path
from typing import IO

_FILE_CAP_BYTES = 1_048_576          # 1 MiB ceiling on stderr.log
_TAIL_BYTES = 2_048                  # ~2 KiB folded into the error-event detail
_TRUNCATION_MARKER = b"\n[stderr truncated]\n"


class StderrSink:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._fh: IO[bytes] | None = None
        self._open_failed = False
        self._bytes_written = 0
        self._truncated = False
        self._closed = False
        self._tail = bytearray()

    def feed(self, text: str) -> None:
        data = text.encode("utf-8", errors="replace")
        # In-memory tail first: it must populate even if the disk write fails,
        # so the error-event detail is never empty when capture happened.
        self._tail.extend(data)
        if len(self._tail) > _TAIL_BYTES:
            del self._tail[: len(self._tail) - _TAIL_BYTES]
        self._write_file(data)

    def _write_file(self, data: bytes) -> None:
        if self._open_failed or self._truncated or self._closed:
            return
        try:
            if self._fh is None:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                self._fh = open(self._path, "ab", buffering=0)
            remaining = _FILE_CAP_BYTES - self._bytes_written
            if remaining <= 0:
                self._mark_truncated()
                return
            chunk = data[:remaining]
            self._fh.write(chunk)
            self._bytes_written += len(chunk)
            if len(data) > remaining:
                self._mark_truncated()
        except OSError:
            self._open_failed = True

    def _mark_truncated(self) -> None:
        if self._truncated:
            return
        self._truncated = True
        try:
            if self._fh is not None:
                self._fh.write(_TRUNCATION_MARKER)
        except OSError:
            self._open_failed = True

    def tail(self) -> str | None:
        if not self._tail:
            return None
        return self._tail.decode("utf-8", errors="replace")

    def close(self) -> None:
        self._closed = True  # a later feed() updates the tail but won't reopen
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None


# Active sink for the codex streaming tee. The codex SDK subprocess spawn is
# monkeypatched globally (codex_subprocess_patch.py), so the patch can't be
# handed a per-run sink directly — it reads the current one from here. claude_code
# does NOT use this; it wires the sink straight into the SDK stderr callback.
current_stderr_sink: contextvars.ContextVar[StderrSink | None] = (
    contextvars.ContextVar("current_stderr_sink", default=None)
)
