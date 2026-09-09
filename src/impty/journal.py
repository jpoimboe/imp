# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""One session's pty journal: `pty-N.raw` holds exactly the bytes read off the
pty master, `pty-N.jsonl` the out-of-band facts the byte stream cannot carry —
spawn geometry, resize, send boundaries, exit."""

from __future__ import annotations

import contextlib
import json
import logging
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, BinaryIO

_log = logging.getLogger(__name__)

# Tail-preserving bound: at most two segments of this plus one whole chunk of overshoot.
_SEGMENT_LIMIT = 4 * 1024 * 1024


def _write_all(fh: BinaryIO, data: bytes) -> None:
    # Unbuffered: one write(2) per call, which the kernel is free to cut short.
    written = 0
    while written < len(data):
        written += fh.write(data[written:])


class Journal:
    """The single write path into `.raw` is output(), so nothing the journal
    itself produces — framing, sent keystrokes, pyte's terminal-query replies —
    can ever appear there. Everything else goes to the sidecar."""

    def __init__(self, directory: Path, index: int) -> None:
        # close() writes `end` after releasing the session's _io_lock, so that lock does not cover
        # every call site; this leaf lock does.
        self._lock = threading.Lock()
        self._t0 = time.monotonic()
        self._off = 0
        self._seg = 0
        self._prev_seg = 0
        self._dropped = 0
        self._raw_path = directory / f"pty-{index}.raw"
        self._prev_path = directory / f"pty-{index}.raw.1"
        self._jsonl_path = directory / f"pty-{index}.jsonl"
        self._raw: BinaryIO | None = None
        self._jsonl: BinaryIO | None = None
        try:
            self._raw = self._raw_path.open("wb", buffering=0)
        except OSError as exc:
            self._disable(self._raw_path, exc)
            return
        try:
            self._jsonl = self._jsonl_path.open("wb", buffering=0)
        except OSError as exc:
            self._disable(self._jsonl_path, exc)

    def output(self, chunk: bytes) -> None:
        """Journal one chunk read off the master."""
        with self._lock:
            if self._raw is None:
                return
            try:
                _write_all(self._raw, chunk)
            except OSError as exc:
                self._disable(self._raw_path, exc)
                return
            self._off += len(chunk)
            self._seg += len(chunk)
            if self._seg >= _SEGMENT_LIMIT:
                self._rotate(self._raw)

    def _rotate(self, raw: BinaryIO) -> None:
        # The check runs after a whole chunk is written, never mid-chunk: the chunk is
        # the atomic unit of what was read.
        # Cleared up front: a failed roll then lands in the same disabled state as a failed open.
        self._raw = None
        try:
            raw.close()
            self._raw_path.replace(self._prev_path)
            self._raw = self._raw_path.open("wb", buffering=0)
        except OSError as exc:
            self._disable(self._raw_path, exc)
            return
        self._dropped += self._prev_seg  # the predecessor this roll overwrote
        self._prev_seg = self._seg
        self._seg = 0
        self._record("rotate", dropped=self._dropped)

    def start(self, argv: Sequence[str], cols: int, rows: int) -> None:
        with self._lock:
            self._record("start", argv=list(argv), cols=cols, rows=rows)

    def resize(self, rows: int, cols: int) -> None:
        with self._lock:
            self._record("resize", cols=cols, rows=rows)

    def send(self, nbytes: int) -> None:
        """Length only, never the content — the boundary is for turn segmentation."""
        with self._lock:
            self._record("send", len=nbytes)

    def end(self, exit_code: int) -> None:
        with self._lock:
            self._record("end", exit=exit_code)
            self._close()

    # _record and _close run with _lock held; _disable too, except from __init__.
    def _record(self, ev: str, **fields: Any) -> None:
        if self._jsonl is None:
            return
        rec: dict[str, Any] = {
            "t": round(time.monotonic() - self._t0, 3), "off": self._off, "ev": ev,
        }
        rec.update(fields)
        line = json.dumps(rec, separators=(",", ":")).encode() + b"\n"
        try:
            _write_all(self._jsonl, line)
        except OSError as exc:
            self._disable(self._jsonl_path, exc)

    def _disable(self, path: Path, exc: OSError) -> None:
        _log.warning("impty: pty journal disabled (%s): %s", path, exc)
        self._close()

    def _close(self) -> None:
        for fh in (self._raw, self._jsonl):
            if fh is not None:
                with contextlib.suppress(OSError):
                    fh.close()
        self._raw = None
        self._jsonl = None
