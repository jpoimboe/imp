"""Claude Code provider - full implementation lands in Phase C (Tasks 16-24).

This stub is enough to satisfy `providers/__init__.py`'s import-time
registration without making `claude_code` advertise as available.
"""

from __future__ import annotations

import shutil

from imp.providers.base import Provider


def _env_check() -> bool:
    """Stub - full check is `shutil.which('claude') is not None` (Task 24)."""
    return shutil.which("claude") is not None


def _factory(model: str) -> Provider:
    raise NotImplementedError(
        f"claude_code provider lands in Phase C of Plan 2 (Tasks 16-24); model={model}"
    )
