# src/imp/providers/stderr_sink.py
"""Bounded capture of a provider CLI subprocess's stderr.

Each provider builds one StderrSink per step from `invocation.transcript_dir`,
feeds the subprocess's stderr into it, and folds `tail()` into the error-event
detail on failure. The full stream lands in `<step>/stderr.log` (capped); the
in-memory tail survives a disk-write failure so the error detail is never lost.
"""
from __future__ import annotations

import contextlib
import contextvars
import re
from collections.abc import Callable
from pathlib import Path
from typing import IO, Any

_FILE_CAP_BYTES = 1_048_576          # 1 MiB ceiling on stderr.log
_TAIL_BYTES = 2_048                  # ~2 KiB folded into the error-event detail
_TRUNCATION_MARKER = b"\n[stderr truncated]\n"

# Extensible signature keyword set, matched case-insensitively. A stderr span is
# a run of text beginning at one of these keywords.
_WARNING_SIGNATURES = ("Warning:", "Error:")
_SIGNATURE_RE = re.compile(
    "|".join(re.escape(sig) for sig in _WARNING_SIGNATURES), re.IGNORECASE)


def _extract_warning_spans(text: str) -> list[str]:
    # Within each line, a span runs from a signature keyword to the next signature
    # keyword or the line end. Splitting per line first excludes unrelated trailing
    # output; the inner keyword split handles claude's concatenated, newline-less
    # warnings (…betas.Warning: Custom…), which sit on a single line.
    spans: list[str] = []
    for line in text.splitlines():
        starts = [m.start() for m in _SIGNATURE_RE.finditer(line)]
        for i, start in enumerate(starts):
            end = starts[i + 1] if i + 1 < len(starts) else len(line)
            spans.append(line[start:end])
    return spans


class StderrSink:
    def __init__(self, path: Path, *,
                 on_warning: Callable[[str], None] | None = None,
                 source: str = "") -> None:
        self._path = path
        self._on_warning = on_warning
        self._source = source
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
                self._fh = open(self._path, "ab", buffering=0)  # noqa: SIM115
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
        already = self._closed
        self._closed = True  # a later feed() updates the tail but won't reopen
        if self._fh is not None:
            with contextlib.suppress(OSError):
                self._fh.close()  # flush to disk before the scan re-reads it
            self._fh = None
        if not already:
            self._emit_warnings()

    def _captured_text(self) -> str:
        # Re-read the FULL captured stream: warnings are emitted at startup and sit
        # at the front, so the ~2 KiB in-memory tail would miss them. Fall back to
        # that tail only if the file read fails.
        try:
            raw = self._path.read_bytes()[:_FILE_CAP_BYTES]
        except OSError:
            return self.tail() or ""
        return raw.decode("utf-8", errors="replace")

    def _emit_warnings(self) -> None:
        if self._on_warning is None:
            return
        text = self._captured_text()
        if not text:
            return
        seen: set[str] = set()
        for span in _extract_warning_spans(text):
            normalized = " ".join(span.split())  # collapse whitespace, strip
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)  # within-step dedup
            label = f"{self._source} stderr: " if self._source else ""
            self._on_warning(f"{label}{normalized}")


def build_stderr_sink(
    path: Path, *, elog: Any, aid: str, source: str,
) -> StderrSink:
    """Wire close()-time Warning/Error span extraction into elog as runner_warning events."""
    def _on_warning(message: str) -> None:
        elog.append(agent_id=aid, type="runner_warning",
                    payload={"message": message})
    return StderrSink(path, on_warning=_on_warning, source=source)


# Active sink for the codex streaming tee. The codex SDK subprocess spawn is
# monkeypatched globally (codex_subprocess_patch.py), so the patch can't be
# handed a per-run sink directly — it reads the current one from here. claude_code
# does NOT use this; it wires the sink straight into the SDK stderr callback.
current_stderr_sink: contextvars.ContextVar[StderrSink | None] = (
    contextvars.ContextVar("current_stderr_sink", default=None)
)
