# tests/test_prompt_completer.py
from __future__ import annotations

import asyncio
from pathlib import Path

from prompt_toolkit.document import Document

from imp.cli.prompt import SlashCompleter


def _skills(tmp_path: Path) -> Path:
    d = tmp_path / "skills" / "user"
    d.mkdir(parents=True)
    (d / "refactor.md").write_text("---\ndescription: Refactor helper\n---\nx")
    (d / "review.md").write_text("x")
    (d / "debug.md").write_text("x")
    return tmp_path / "skills"


def _metas(completer: SlashCompleter, text: str) -> dict[str, str]:
    doc = Document(text=text, cursor_position=len(text))
    return {c.text: c.display_meta_text for c in completer.get_completions(doc, None)}


def _texts(completer: SlashCompleter, text: str) -> list[str]:
    doc = Document(text=text, cursor_position=len(text))
    return [c.text for c in completer.get_completions(doc, None)]


def test_completes_skill_names_by_prefix(tmp_path: Path) -> None:
    c = SlashCompleter(_skills(tmp_path))
    assert set(_texts(c, "/re")) == {"refactor", "review"}


def test_includes_builtins(tmp_path: Path) -> None:
    c = SlashCompleter(_skills(tmp_path))
    assert "help" in _texts(c, "/h")
    assert "skills" in _texts(c, "/s")


def test_no_completion_for_non_slash(tmp_path: Path) -> None:
    c = SlashCompleter(_skills(tmp_path))
    assert _texts(c, "hello") == []


def test_missing_skills_dir_only_builtins(tmp_path: Path) -> None:
    c = SlashCompleter(tmp_path / "nope")
    assert set(_texts(c, "/")) >= {"help", "skills"}


def test_async_path_delegates_to_get_completions(tmp_path: Path) -> None:
    """prompt_toolkit's async REPL (prompt_async) drives completions through
    get_completions_async, not get_completions. The duck-typed completer must
    provide it or the async event loop crashes with AttributeError."""
    c = SlashCompleter(_skills(tmp_path))
    doc = Document(text="/re", cursor_position=3)

    async def _collect() -> list[str]:
        return [item.text async for item in c.get_completions_async(doc, None)]

    assert set(asyncio.run(_collect())) == {"refactor", "review"}


def test_completion_carries_description_as_meta(tmp_path: Path) -> None:
    c = SlashCompleter(_skills(tmp_path))
    metas = _metas(c, "/re")
    assert metas["refactor"] == "Refactor helper"
    assert metas["review"] == ""   # no description -> empty meta


def test_agent_skills_never_appear_in_completion(tmp_path: Path) -> None:
    skills = _skills(tmp_path)
    agent = skills / "agent"
    agent.mkdir()
    (agent / "secret.md").write_text("x")
    c = SlashCompleter(skills)
    assert "secret" not in _texts(c, "/s")


def test_user_skill_named_like_builtin_not_duplicated(tmp_path: Path) -> None:
    skills = _skills(tmp_path)
    (skills / "user" / "help.md").write_text("x")
    c = SlashCompleter(skills)
    assert _texts(c, "/help").count("help") == 1
