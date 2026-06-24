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
async def test_clear_loop_reopens_only_on_clear(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    intents = iter(["clear", "save"])
    calls: list[str] = []

    async def _fake_cleared(control, base, *, tui, leave_state):
        kind = next(intents)
        calls.append(f"cleared:{kind}")
        leave_state["intent"] = kind
        return {"clear": 0, "save": 5}[kind]

    monkeypatch.setattr(m, "_run_cleared_session", _fake_cleared)
    monkeypatch.setattr(m.worktree, "current_branch", lambda c: "main")

    async def _first(leave_state: dict[str, str]) -> int:
        calls.append("first")
        leave_state["intent"] = "clear"
        return 9

    rc = await m._clear_loop(tmp_path, tui=False, first=_first)

    assert rc == 5                                        # the final non-clear rc
    assert calls == ["first", "cleared:clear", "cleared:save"]  # looped twice


@pytest.mark.anyio
async def test_clear_loop_returns_first_rc_without_clear(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _boom(*a: object, **k: object) -> int:
        raise AssertionError("must not reopen without /clear")

    monkeypatch.setattr(m, "_run_cleared_session", _boom)

    async def _first(leave_state: dict[str, str]) -> int:
        return 3

    assert await m._clear_loop(tmp_path, tui=False, first=_first) == 3


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


@pytest.mark.anyio
async def test_clear_loop_exits_when_cleared_session_sets_no_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    call_count = 0

    async def _fake_cleared(control, base, *, tui, leave_state):
        nonlocal call_count
        call_count += 1
        assert call_count <= 3, "loop did not exit — leave_state reset is missing"
        # intentionally does NOT set leave_state["intent"]
        return 7

    monkeypatch.setattr(m, "_run_cleared_session", _fake_cleared)
    monkeypatch.setattr(m.worktree, "current_branch", lambda c: "main")

    async def _first(leave_state: dict[str, str]) -> int:
        leave_state["intent"] = "clear"
        return 9

    rc = await m._clear_loop(tmp_path, tui=False, first=_first)

    assert rc == 7
    assert call_count == 1


@pytest.mark.anyio
async def test_clear_loop_propagates_exception_from_cleared_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_cleared(control, base, *, tui, leave_state):
        raise RuntimeError("boom")

    monkeypatch.setattr(m, "_run_cleared_session", _fake_cleared)
    monkeypatch.setattr(m.worktree, "current_branch", lambda c: "main")

    async def _first(leave_state: dict[str, str]) -> int:
        leave_state["intent"] = "clear"
        return 0

    with pytest.raises(RuntimeError, match="boom"):
        await m._clear_loop(tmp_path, tui=False, first=_first)
