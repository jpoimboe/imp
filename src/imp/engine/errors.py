"""Error types with file:line context."""

from __future__ import annotations

from dataclasses import dataclass

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


@dataclass
class LoadErrors(Exception):
    """Aggregated LoadErrors reported together at end of load."""
    errors: list[LoadError]

    def __str__(self) -> str:
        return "\n\n".join(str(e) for e in self.errors)
