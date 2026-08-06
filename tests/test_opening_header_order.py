from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import imp.cli.main as main


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_read_opening_message_prints_run_header_before_reading(
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen: dict[str, str] = {}

    class _FakePrompt:
        async def ask_async(self, **kwargs: object) -> str:
            # Everything printed up to the moment the prompt starts reading.
            seen["before_read"] = capsys.readouterr().out
            return "  add oauth  "

    quest = SimpleNamespace(id="z9y8x7", title="",
                            control_root=Path("/checkouts/imp"))
    reply = await main._read_opening_message(
        quest, leave_mode="new", banner_shown=False, color=False,
        width=80, editing_mode="emacs",
        _prompt_factory=lambda: _FakePrompt())

    assert reply == "add oauth"                    # normalized (stripped)
    assert "z9y8x7" in seen["before_read"]         # header printed BEFORE the read


@pytest.mark.anyio
async def test_read_opening_message_reprompts_on_empty_reply() -> None:
    # A whitespace-only submit re-prompts silently; only the real line returns.
    class _FakePrompt:
        def __init__(self) -> None:
            self._lines = iter(["   ", "real intent"])

        async def ask_async(self, **kwargs: object) -> str:
            return next(self._lines)

    quest = SimpleNamespace(id="aa11bb", title="",
                            control_root=Path("/checkouts/imp"))
    reply = await main._read_opening_message(
        quest, leave_mode="new", banner_shown=False, color=False,
        width=80, editing_mode="emacs",
        _prompt_factory=lambda: _FakePrompt())
    assert reply == "real intent"


def test_print_opening_leads_with_header_then_splash(monkeypatch, capsys) -> None:
    monkeypatch.setattr(main.splash, "splash_block", lambda **_k: "SCENE")
    monkeypatch.setattr(main, "_term_width", lambda: 80)
    quest = SimpleNamespace(id="ab12cd", title="")
    shown = main._print_opening(quest, "new", interactive=True, color=False)
    out = capsys.readouterr().out
    assert shown is True
    assert out == "ab12cd · new\n\nSCENE\n\n"
    assert out.index("ab12cd") < out.index("SCENE")


def test_print_opening_skips_splash_when_not_interactive(monkeypatch, capsys) -> None:
    monkeypatch.setattr(main.splash, "splash_block", lambda **_k: "SCENE")
    quest = SimpleNamespace(id="ab12cd", title="Dark mode")
    shown = main._print_opening(quest, "new", interactive=False, color=False)
    out = capsys.readouterr().out
    assert shown is False
    assert out == "ab12cd · Dark mode · new\n\n"
    assert "SCENE" not in out


@pytest.mark.anyio
async def test_read_opening_message_leads_with_header_then_splash_then_prompt(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(main.splash, "splash_block", lambda **_k: "SCENE")
    monkeypatch.setattr(main, "_term_width", lambda: 80)
    seen: dict[str, str] = {}

    class _FakePrompt:
        async def ask_async(self, **kwargs: object) -> str:
            seen["before_read"] = capsys.readouterr().out
            return "add oauth"

    quest = SimpleNamespace(id="z9y8x7", title="",
                            control_root=Path("/checkouts/imp"))
    reply = await main._read_opening_message(
        quest, leave_mode="new", banner_shown=True, color=False,
        width=80, editing_mode="emacs", _prompt_factory=lambda: _FakePrompt())

    assert reply == "add oauth"
    assert seen["before_read"] == "z9y8x7 · new\n\nSCENE\n\n"
