from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from imp.agents.tools.wait_for_user import AwaitingUserInput
from imp.cli import main as cli_main


def test_build_parser_new_accepts_spec_file() -> None:
    ns = cli_main._build_parser().parse_args(["new", "--spec-file", "draft.md"])
    assert ns.spec_file == "draft.md"


def test_build_parser_new_spec_file_defaults_none() -> None:
    ns = cli_main._build_parser().parse_args(["new"])
    assert ns.spec_file is None


def _new_args(**kw: object) -> argparse.Namespace:
    base = {"title": None, "non_interactive": False, "message": None,
            "tui": False, "spec_file": None}
    base.update(kw)
    return argparse.Namespace(**base)


def test_resolve_positional_file_is_seed(tmp_path: Path) -> None:
    draft = tmp_path / "draft.md"
    draft.write_text("# Draft\nGoal: do the thing\n")
    res = cli_main.resolve_spec_seed(_new_args(title=str(draft)))
    assert res.title is None
    assert res.seed_text is not None
    assert res.seed_text.startswith("Implement this spec")
    assert str(draft) in res.seed_text
    assert "Goal: do the thing" in res.seed_text


def test_resolve_positional_nonfile_is_title() -> None:
    res = cli_main.resolve_spec_seed(_new_args(title="just a title"))
    assert res == cli_main.SeedResolution(title="just a title", seed_text=None)


def test_resolve_no_positional_is_empty() -> None:
    res = cli_main.resolve_spec_seed(_new_args())
    assert res == cli_main.SeedResolution(title=None, seed_text=None)


def test_resolve_spec_file_keeps_positional_title(tmp_path: Path) -> None:
    draft = tmp_path / "draft.md"
    draft.write_text("Goal: ship it\n")
    res = cli_main.resolve_spec_seed(
        _new_args(title="my-name", spec_file=str(draft)))
    assert res.title == "my-name"
    assert res.seed_text is not None
    assert str(draft) in res.seed_text
    assert "Goal: ship it" in res.seed_text


def test_resolve_missing_spec_file_errors(tmp_path: Path) -> None:
    with pytest.raises(cli_main.SeedError):
        cli_main.resolve_spec_seed(
            _new_args(spec_file=str(tmp_path / "nope.md")))


def test_resolve_spec_file_and_message_mutually_exclusive(tmp_path: Path) -> None:
    draft = tmp_path / "draft.md"
    draft.write_text("Goal: x\n")
    with pytest.raises(cli_main.SeedError):
        cli_main.resolve_spec_seed(
            _new_args(spec_file=str(draft), message="hi"))


def test_resolve_positional_file_and_message_mutually_exclusive(
        tmp_path: Path) -> None:
    draft = tmp_path / "draft.md"
    draft.write_text("Goal: x\n")
    with pytest.raises(cli_main.SeedError):
        cli_main.resolve_spec_seed(
            _new_args(title=str(draft), message="hi"))


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_seed_reply_first_then_fallback() -> None:
    async def _fallback() -> str:
        return "interactive line"

    reply = cli_main.make_seed_reply_fn("the seed", _fallback)
    assert await reply() == "the seed"          # first solicit → seed
    assert await reply() == "interactive line"  # later solicit → fallback
    assert await reply() == "interactive line"  # still fallback on third call


@pytest.mark.anyio
async def test_seed_reply_then_headless_pause() -> None:
    reply = cli_main.make_seed_reply_fn(
        "the seed", cli_main.make_message_reply_fn(None))
    assert await reply() == "the seed"          # first solicit → seed
    with pytest.raises(AwaitingUserInput):       # headless fallback → pause
        await reply()
