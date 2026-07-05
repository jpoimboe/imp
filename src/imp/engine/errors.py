"""Error types with file:line context."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from imp.engine.types import SourceLoc


@dataclass
class LoadError(Exception):
    """A workflow/agent loading or validation error. Carries a source location
    and an optional 'hint' line for fix suggestions.
    """
    message: str
    source: SourceLoc | None = None
    hint: str | None = None

    def __str__(self) -> str:
        parts = []
        if self.source is not None:
            parts.append(f"{self.source}\n  {self.message}")
        else:
            parts.append(self.message)
        if self.hint:
            parts.append(f"  hint: {self.hint}")
        return "\n".join(parts)

    @property
    def errors(self) -> list[LoadError]:
        return [self]


@dataclass
class LoadErrors(Exception):
    """Aggregated errors + warnings reported together at end of load."""
    errors: list[LoadError]
    warnings: list[LoadError]

    def __str__(self) -> str:
        chunks = []
        if self.errors:
            chunks.append("errors:\n" + "\n\n".join(str(e) for e in self.errors))
        if self.warnings:
            chunks.append("warnings:\n" + "\n\n".join(str(w) for w in self.warnings))
        return "\n\n".join(chunks)


class LayoutMismatch(LoadError):
    """Raised on resume when stored layout_version differs from the engine's."""

    def __init__(self, *, stored: int, engine: int, imp_commit: str,
                 change_dir: Path) -> None:
        self.stored = stored
        self.engine = engine
        self.imp_commit = imp_commit
        self.change_dir = change_dir
        breadcrumb = imp_commit or "(unknown)"
        super().__init__(
            f"Change at {change_dir} has layout_version {stored}, but this imp "
            f"engine speaks layout_version {engine}; refusing to resume it under "
            f"a contradictory engine.\n"
            f"  birth imp_commit: {breadcrumb}\n"
            f"  recover: finish or abandon-and-restart this change under the new "
            f"layout, or hand-migrate {change_dir}; its events and transcripts "
            f"are accessible via `imp log {change_dir.name}`."
        )


class StateDecodeError(LoadError):
    """Raised when `State.load` cannot parse `state.json`."""

    def __init__(self, *, live: Path, sidecar: Path | None, reason: str) -> None:
        self.live = live
        self.sidecar = sidecar
        self.reason = reason
        if sidecar is not None:
            evidence = f"  preserved corrupt bytes at: {sidecar}"
        else:
            evidence = (
                "  corrupt bytes could NOT be preserved "
                "(sidecar write failed)"
            )
        super().__init__(
            f"could not parse {live}: {reason}\n{evidence}",
            hint="restore a good state.json from backup, or "
                 "abandon-and-restart this change.",
        )


def _decode_stream(stream: object) -> str:
    """Normalize a subprocess stream (bytes / str / None) to text."""
    if stream is None:
        return ""
    if isinstance(stream, bytes):
        return stream.decode(errors="replace")
    return str(stream)


def _stream_tail(lines: list[str], *, max_lines: int = 15,
                 max_chars: int = 2048) -> str:
    trimmed = list(lines)
    while trimmed and not trimmed[-1].strip():
        trimmed.pop()
    window = trimmed[-max_lines:]
    kept: list[str] = []
    total = 0
    for ln in reversed(window):
        added = len(ln) + (1 if kept else 0)
        if kept and total + added > max_chars:
            break
        kept.append(ln)
        total += added
    return "\n".join(reversed(kept))[-max_chars:]


def summarize_exception(e: BaseException) -> tuple[str, dict[str, Any]]:
    """Return (message, detail) for an engine error.

    Default: (first line of str(e), {}). For a subprocess.CalledProcessError,
    append the last non-empty stderr line to the message and put a capped tail
    of that stream in detail["stderr_tail"]; when stderr is empty/absent, fall
    back to stdout the same way (still under the stderr_tail key).
    """
    first = (str(e).splitlines() or [""])[0]
    if not isinstance(e, subprocess.CalledProcessError):
        return first, {}
    for stream in (e.stderr, e.stdout):
        text = _decode_stream(stream)
        all_lines = text.splitlines()
        nonempty = [ln for ln in all_lines if ln.strip()]
        if nonempty:
            return (
                f"{first} — {nonempty[-1]}",
                {"stderr_tail": _stream_tail(all_lines)},
            )
    return first, {}
