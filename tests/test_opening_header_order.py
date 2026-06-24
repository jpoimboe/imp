from __future__ import annotations

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

    change = SimpleNamespace(id="z9y8x7", title="")
    reply = await main._read_opening_message(
        change, leave_mode="new", banner_shown=False, color=False,
        width=80, editing_mode="emacs",
        _prompt_factory=lambda: _FakePrompt())

    assert reply == "add oauth"                    # normalized (stripped)
    assert "z9y8x7" in seen["before_read"]         # header printed BEFORE the read


@pytest.mark.anyio
async def test_read_opening_message_returns_none_for_empty_reply() -> None:
    class _FakePrompt:
        async def ask_async(self, **kwargs: object) -> str:
            return "   "

    change = SimpleNamespace(id="aa11bb", title="")
    reply = await main._read_opening_message(
        change, leave_mode="new", banner_shown=False, color=False,
        width=80, editing_mode="emacs",
        _prompt_factory=lambda: _FakePrompt())
    assert reply is None
