# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
from __future__ import annotations

import re
from pathlib import Path

import pytest

from imp.providers.paths import claude_canonical_dir, codex_canonical_dir


def test_claude_canonical_dir_honors_claude_config_dir_and_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert claude_canonical_dir({"CLAUDE_CONFIG_DIR": str(tmp_path)}) == tmp_path
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    assert claude_canonical_dir({}) == tmp_path / ".claude"
    assert claude_canonical_dir({"CLAUDE_CONFIG_DIR": ""}) == tmp_path / ".claude"


def test_codex_canonical_dir_honors_codex_home_and_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert codex_canonical_dir({"CODEX_HOME": str(tmp_path)}) == tmp_path
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    assert codex_canonical_dir({}) == tmp_path / ".codex"
    assert codex_canonical_dir({"CODEX_HOME": ""}) == tmp_path / ".codex"


def test_each_canonical_dir_is_defined_once_in_provider_paths() -> None:
    src = Path(__file__).resolve().parents[1] / "src" / "imp"
    defs = sorted(
        (p.relative_to(src).as_posix(), name)
        for p in src.rglob("*.py")
        for name in re.findall(
            r"^\s*def (\w*canonical_dir)\(", p.read_text(encoding="utf-8"), re.M)
    )
    assert defs == [
        ("providers/paths.py", "claude_canonical_dir"),
        ("providers/paths.py", "codex_canonical_dir"),
    ]
