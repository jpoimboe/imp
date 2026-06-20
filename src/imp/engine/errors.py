"""Error types with file:line context."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

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
            f"are preserved at refs/imp/journal "
            f"(git cat-file -p refs/imp/journal:<id>/events.jsonl)."
        )
