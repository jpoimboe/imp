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
    # /re matches the resume builtin alongside the refactor/review skills.
    assert set(_texts(c, "/re")) == {"refactor", "review", "resume"}


def test_includes_builtins(tmp_path: Path) -> None:
    c = SlashCompleter(_skills(tmp_path))
    assert set(_texts(c, "/")) >= {
        "help", "skills", "list", "search", "save", "clear", "abandon", "resume"}
    # An unambiguous prefix fills exactly one command (no skill shares these stems).
    assert set(_texts(c, "/sav")) == {"save"}
    assert set(_texts(c, "/san")) == {"sandbox"}
    assert set(_texts(c, "/se")) == {"search"}
    assert set(_texts(c, "/ab")) == {"abandon"}


def test_no_completion_for_non_slash(tmp_path: Path) -> None:
    c = SlashCompleter(_skills(tmp_path))
    assert _texts(c, "hello") == []


def test_missing_skills_dir_only_builtins(tmp_path: Path) -> None:
    c = SlashCompleter(tmp_path / "nope")
    assert set(_texts(c, "/")) == {
        "help", "skills", "list", "search", "save", "clear", "model", "sandbox",
        "abandon", "resume"}


def test_async_path_delegates_to_get_completions(tmp_path: Path) -> None:
    """prompt_toolkit's async REPL (prompt_async) drives completions through
    get_completions_async, not get_completions. The duck-typed completer must
    provide it or the async event loop crashes with AttributeError."""
    c = SlashCompleter(_skills(tmp_path))
    doc = Document(text="/re", cursor_position=3)

    async def _collect() -> list[str]:
        return [item.text async for item in c.get_completions_async(doc, None)]

    assert set(asyncio.run(_collect())) == {"refactor", "review", "resume"}


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


def test_builtins_include_model() -> None:
    assert "model" in SlashCompleter._BUILTINS


_ROSTER = [
    "claude-haiku-4-5", "claude-sonnet-4-6", "claude-opus-4-8",
    "claude-opus-4-8[1m]", "claude-fable-5", "opus", "haiku", "sonnet",
]


def _completions(completer: SlashCompleter, text: str) -> list:
    doc = Document(text=text, cursor_position=len(text))
    return list(completer.get_completions(doc, None))


def test_model_arg_completes_full_set_including_aliases(tmp_path: Path) -> None:
    c = SlashCompleter(_skills(tmp_path), roster_models=lambda: list(_ROSTER))
    assert set(_texts(c, "/model ")) == set(_ROSTER)


def test_model_arg_prefix_narrows(tmp_path: Path) -> None:
    c = SlashCompleter(_skills(tmp_path), roster_models=lambda: list(_ROSTER))
    # bare-alias prefix: only the alias starts with "op"
    assert set(_texts(c, "/model op")) == {"opus"}
    # dated-id prefix pulls both opus variants but not the bare alias
    assert set(_texts(c, "/model claude-o")) == {
        "claude-opus-4-8", "claude-opus-4-8[1m]"}


def test_model_arg_start_position_and_empty_meta(tmp_path: Path) -> None:
    c = SlashCompleter(_skills(tmp_path), roster_models=lambda: ["opus"])
    comps = _completions(c, "/model op")
    assert [(x.text, x.start_position, x.display_meta_text) for x in comps] == [
        ("opus", -2, "")]


def test_model_source_read_at_most_once(tmp_path: Path) -> None:
    calls = {"n": 0}

    def source() -> list[str]:
        calls["n"] += 1
        return ["opus", "haiku"]

    c = SlashCompleter(_skills(tmp_path), roster_models=source)
    _texts(c, "/model ")
    _texts(c, "/model op")
    _texts(c, "/model h")
    assert calls["n"] == 1


def test_no_roster_source_yields_no_model_arg_completions(tmp_path: Path) -> None:
    c = SlashCompleter(_skills(tmp_path))  # no roster source threaded
    assert _texts(c, "/model ") == []


def test_command_name_completion_unaffected_by_roster_source(tmp_path: Path) -> None:
    c = SlashCompleter(_skills(tmp_path), roster_models=lambda: ["opus"])
    # no space yet -> still the command-name branch
    assert "model" in _texts(c, "/mod")
    assert "model" in _texts(c, "/model")
    # a non-model command with an argument yields nothing (unchanged behavior)
    assert _texts(c, "/search foo") == []
