from __future__ import annotations

import argparse
from pathlib import Path

import anyio
import pytest

from imp.agents.tools.wait_for_user import AwaitingUserInput
from imp.cli import main as cli_main
from imp.engine.executor import RunResult
from tests.conftest import _seed_control


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


def test_new_positional_file_delivers_seed(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    draft = tmp_path / "draft.md"
    draft.write_text("# My draft\nGoal: do X\n")

    captured: dict[str, object] = {}

    async def _fake_run(change: object, *, on_event: object,
                        user_chat_input_fn: object, coordinator: object,
                        non_interactive: object) -> RunResult:
        captured["change"] = change
        captured["reply"] = await user_chat_input_fn(None)  # type: ignore[operator]
        return RunResult.COMPLETED

    monkeypatch.setattr(cli_main, "run_change", _fake_run)

    args = _new_args(title=str(draft), non_interactive=True)
    rc = anyio.run(cli_main._async_cmd_new, args)

    assert rc == 0
    reply = captured["reply"]
    assert isinstance(reply, str)
    assert reply.startswith("Implement this spec")
    assert str(draft) in reply
    assert "# My draft" in reply
    assert captured["change"].title == ""  # type: ignore[union-attr]


def test_new_spec_file_with_name_keeps_title(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    draft = tmp_path / "draft.md"
    draft.write_text("Goal: ship it\n")

    captured: dict[str, object] = {}

    async def _fake_run(change: object, *, on_event: object,
                        user_chat_input_fn: object, coordinator: object,
                        non_interactive: object) -> RunResult:
        captured["change"] = change
        captured["reply"] = await user_chat_input_fn(None)  # type: ignore[operator]
        return RunResult.COMPLETED

    monkeypatch.setattr(cli_main, "run_change", _fake_run)

    args = _new_args(title="my-name", spec_file=str(draft), non_interactive=True)
    rc = anyio.run(cli_main._async_cmd_new, args)

    assert rc == 0
    assert captured["change"].title == "my-name"  # type: ignore[union-attr]
    assert "Goal: ship it" in captured["reply"]  # type: ignore[operator]


def test_new_missing_spec_file_rc2_no_change(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    ran = {"called": False}

    async def _fake_run(*_a: object, **_k: object) -> RunResult:
        ran["called"] = True
        return RunResult.COMPLETED

    monkeypatch.setattr(cli_main, "run_change", _fake_run)

    args = _new_args(spec_file=str(tmp_path / "nope.md"), non_interactive=True)
    rc = anyio.run(cli_main._async_cmd_new, args)

    assert rc == 2
    assert ran["called"] is False
    assert "nope.md" in capsys.readouterr().err
    worktrees = tmp_path / ".imp" / "worktrees"
    assert not worktrees.exists() or not any(worktrees.iterdir())


def test_new_spec_file_and_message_rc2(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    draft = tmp_path / "draft.md"
    draft.write_text("Goal: x\n")
    ran = {"called": False}

    async def _fake_run(*_a: object, **_k: object) -> RunResult:
        ran["called"] = True
        return RunResult.COMPLETED

    monkeypatch.setattr(cli_main, "run_change", _fake_run)

    args = _new_args(spec_file=str(draft), message="hi", non_interactive=True)
    rc = anyio.run(cli_main._async_cmd_new, args)

    assert rc == 2
    assert ran["called"] is False
    assert "mutually exclusive" in capsys.readouterr().err


def test_new_tui_delivers_seed(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    draft = tmp_path / "draft.md"
    draft.write_text("Goal: x\n")

    captured: dict[str, object] = {}

    async def _fake_tui(change: object, *, args: object, seed: str | None = None) -> int:
        captured["seed"] = seed
        return 0

    monkeypatch.setattr(cli_main, "_run_tui_session", _fake_tui)
    monkeypatch.setattr(cli_main, "_tui_requested", lambda _a: True)

    args = _new_args(title=str(draft), tui=True)
    rc = anyio.run(cli_main._async_cmd_new, args)

    assert rc == 0
    # The seed is delivered to the TUI session, not dropped with a warning.
    seed_text = captured["seed"]
    assert isinstance(seed_text, str)
    assert seed_text.startswith("Implement this spec")
    assert "Goal: x" in seed_text
    assert "--tui does not deliver" not in capsys.readouterr().err


@pytest.mark.anyio
async def test_seed_gate_first_then_per_call_fallback() -> None:
    calls: list[str] = []

    async def _f1() -> str:
        calls.append("f1")
        return "first-fallback"

    async def _f2() -> str:
        calls.append("f2")
        return "second-fallback"

    gate = cli_main.make_seed_gate("the seed")
    assert await gate(_f1) == "the seed"           # first solicit → seed
    assert calls == []                             # fallback never consulted
    assert await gate(_f2) == "second-fallback"    # later solicit → per-call fallback
    assert await gate(_f1) == "first-fallback"     # still defers, to the new per-call fn
    assert calls == ["f2", "f1"]                    # exactly the fallbacks passed per call


@pytest.mark.anyio
async def test_seed_gate_over_attended_race_is_deterministic() -> None:
    from imp.cli.remote_channel import effective_reply_fn

    class _FakeChannel:
        reply_timeout = None

        async def next_reply(self, *, timeout: float | None = None) -> str:
            return "from-driver"

    async def _local_never() -> str:
        await anyio.sleep_forever()
        raise AssertionError("unreachable")

    raced = effective_reply_fn(_local_never, _FakeChannel(), non_interactive=False)
    gate = cli_main.make_seed_gate("the seed")
    with anyio.fail_after(5):
        assert await gate(raced) == "the seed"      # seed without winning a race
        assert await gate(raced) == "from-driver"   # later solicit → race resolves


def test_spec_writer_prompt_has_seed_branch() -> None:
    # tests/ sits directly under the repo root, so resolve the prompt relative
    # to this file rather than depending on project-root discovery.
    root = Path(__file__).resolve().parent.parent
    prompt = (root / "src" / "imp" / "assets" / "workflows" / "default"
              / "agents" / "spec-writer.md").read_text()
    # Seed-marker invariant: the adopt branch must key on the SAME phrase the CLI
    # prefixes (_build_seed). If either drifts, the branch stops firing. The
    # marker must live INSIDE the seeded section, not merely somewhere in the
    # file — assert it appears after the heading (and before the next ## heading).
    heading = "## Seeded with a draft spec"
    assert heading in prompt
    section = prompt[prompt.index(heading):]
    next_heading = section.find("\n## ", len(heading))
    if next_heading != -1:
        section = section[:next_heading]
    assert cli_main.SPEC_SEED_MARKER in section
