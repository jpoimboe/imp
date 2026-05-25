"""Subagent compilation."""

from __future__ import annotations

from pathlib import Path

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel

from imp.agents.subagents import compile_subagent_specs
from imp.engine.loader import parse_agent_file


def test_no_subagents_returns_empty(tmp_path: Path) -> None:
    parent = tmp_path / "parent.md"
    parent.write_text(
        "---\nname: parent\ntools: []\n"
        "output:\n  status: {type: enum, values: [Success]}\n"
        "---\nyou are parent\n"
    )
    out = compile_subagent_specs(
        parse_agent_file(parent), agents_dir=tmp_path,
        model=FakeListChatModel(responses=["ok"]),
    )
    assert out == []


def test_resolves_subagent_files(tmp_path: Path) -> None:
    (tmp_path / "child.md").write_text(
        "---\nname: child\ntools: []\n"
        "output:\n  status: {type: enum, values: [Success]}\n"
        "---\nyou are child\n"
    )
    (tmp_path / "parent.md").write_text(
        "---\nname: parent\ntools: [task]\nsubagents: [child]\n"
        "output:\n  status: {type: enum, values: [Success]}\n"
        "---\nyou are parent\n"
    )
    out = compile_subagent_specs(
        parse_agent_file(tmp_path / "parent.md"), agents_dir=tmp_path,
        model=FakeListChatModel(responses=["ok"]),
    )
    assert len(out) == 1
    spec = out[0]
    name = spec["name"] if isinstance(spec, dict) else getattr(spec, "name", None)
    assert name == "child"


def test_missing_subagent_raises(tmp_path: Path) -> None:
    (tmp_path / "parent.md").write_text(
        "---\nname: parent\ntools: [task]\nsubagents: [missing]\n"
        "output:\n  status: {type: enum, values: [Success]}\n"
        "---\nyou are parent\n"
    )
    with pytest.raises(FileNotFoundError):
        compile_subagent_specs(
            parse_agent_file(tmp_path / "parent.md"), agents_dir=tmp_path,
            model=FakeListChatModel(responses=["ok"]),
        )
