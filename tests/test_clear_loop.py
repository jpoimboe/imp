from __future__ import annotations

from pathlib import Path

import pytest

import imp.cli.main as m
from tests.conftest import _seed_control


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_run_cleared_session_opens_fresh_new_draft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)

    splash_calls: list[bool] = []

    def _fake_splash(*, interactive: bool) -> bool:
        splash_calls.append(interactive)
        return False

    monkeypatch.setattr(m, "_print_splash_banner", _fake_splash)

    class _FakePrompt:
        def __init__(self, *a: object, **k: object) -> None: ...
        async def ask_async(self, *a: object, **k: object) -> str:
            return "build a parser"

    monkeypatch.setattr(m, "Prompt", _FakePrompt)
    monkeypatch.setattr(m, "make_interactive_solicit",
                        lambda *a, **k: (lambda _p: None))

    captured: dict[str, object] = {}

    async def _fake_client(change: object, **kwargs: object) -> int:
        captured["change"] = change
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(m, "run_session_client", _fake_client)

    ls: dict[str, str] = {}
    rc = await m._run_cleared_session(tmp_path, "main", tui=False, leave_state=ls)

    assert rc == 0
    assert splash_calls == [True]                     # the splash was re-rendered (AC 5b)
    assert captured["leave_mode"] == "new"            # parks like imp new, not abandon
    assert captured["seed_opening"] == "build a parser"   # opening prompt seeded the chat
    assert captured["leave_state"] is ls              # threads the loop's dict
    change = captured["change"]
    assert (tmp_path / ".imp" / "changes" / change.id).exists()  # a brand-new draft home
    assert change.title == ""                          # imp new-style: unnamed draft


@pytest.mark.anyio
@pytest.mark.parametrize("exc", [EOFError, KeyboardInterrupt])
async def test_run_cleared_session_cancel_returns_130_no_draft(
    exc: type[BaseException], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)

    monkeypatch.setattr(m, "_print_splash_banner", lambda *, interactive: False)

    class _FakePromptCancel:
        def __init__(self, *a: object, **k: object) -> None: ...
        async def ask_async(self, *a: object, **k: object) -> str:
            raise exc()

    monkeypatch.setattr(m, "Prompt", _FakePromptCancel)

    rc = await m._run_cleared_session(tmp_path, "main", tui=False, leave_state={})

    assert rc == 130
    changes_dir = tmp_path / ".imp" / "changes"
    entries = list(changes_dir.iterdir()) if changes_dir.exists() else []
    assert entries == []


@pytest.mark.anyio
async def test_run_cleared_session_tui_arm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)

    monkeypatch.setattr(m, "_print_splash_banner", lambda *, interactive: False)

    class _FakePrompt:
        def __init__(self, *a: object, **k: object) -> None: ...
        async def ask_async(self, *a: object, **k: object) -> str:
            return "build a parser"

    monkeypatch.setattr(m, "Prompt", _FakePrompt)

    captured: dict[str, object] = {}

    async def _fake_tui_session(change: object, **kwargs: object) -> int:
        captured["change"] = change
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(m, "_run_tui_session", _fake_tui_session)

    ls: dict[str, str] = {}
    rc = await m._run_cleared_session(tmp_path, "main", tui=True, leave_state=ls)

    assert rc == 0
    assert captured["leave_mode"] == "new"
    assert captured["leave_state"] is ls
    assert captured["seed_opening"] == "build a parser"
    assert captured["opening_intent"] == "build a parser"
