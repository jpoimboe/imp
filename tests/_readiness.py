# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""Pin every local sign-in input so readiness never reads the host's logins or CLIs."""
from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import pytest

_LOGIN_VARS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")


def pin_readiness(
    monkeypatch: pytest.MonkeyPatch, base: Path, *,
    on_path: Iterable[str] = ("claude", "codex"),
    keys: Iterable[str] = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"),
) -> tuple[Path, Path]:
    """Only `on_path` CLIs are found, only `keys` are set, and CLAUDE_CONFIG_DIR /
    CODEX_HOME are empty dirs under `base`; returns (claude dir, codex dir)."""
    found = frozenset(on_path)
    monkeypatch.setattr(
        "imp.providers.which", lambda name: f"/pinned/bin/{name}" if name in found else None)
    claude_dir, codex_dir = base / "claude-config", base / "codex-home"
    claude_dir.mkdir(parents=True, exist_ok=True)
    codex_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_dir))
    monkeypatch.setenv("CODEX_HOME", str(codex_dir))
    wanted = set(keys)
    for var in wanted:
        monkeypatch.setenv(var, "sk-test")
    for var in set(_LOGIN_VARS) - wanted:
        monkeypatch.delenv(var, raising=False)
    return claude_dir, codex_dir
