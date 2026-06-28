from __future__ import annotations

import argparse
import functools
import io
import sys
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest

import imp.cli.main as m
import imp.cli.signals as sig
from imp.engine.change import Change
from imp.engine.state import State
from tests.conftest import _seed_control


class _TTY(io.StringIO):
    def isatty(self) -> bool:
        return True


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


def test_new_interactive_routes_through_clear_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)

    seen: dict[str, object] = {}

    async def _fake_loop(control, *, tui, first):
        seen["tui"] = tui
        return 0

    monkeypatch.setattr(m, "_clear_loop", _fake_loop)

    orig_in, orig_out = sys.stdin.isatty, sys.stdout.isatty
    sys.stdin.isatty = lambda: True   # type: ignore[method-assign]
    sys.stdout.isatty = lambda: True  # type: ignore[method-assign]
    try:
        rc = anyio.run(m._async_cmd_new, argparse.Namespace(
            title=None, opening="add a thing", non_interactive=False,
            message=None, tui=False))
    finally:
        sys.stdin.isatty = orig_in    # type: ignore[method-assign]
        sys.stdout.isatty = orig_out  # type: ignore[method-assign]

    assert rc == 0
    assert seen["tui"] is False        # interactive line client → line modality


def test_resume_tui_routes_through_clear_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    ch = Change.create(tmp_path, title="demo", change_id="feat", base_branch="main")
    st = State.load(ch.state_dir)
    st.status = "paused"
    st.save(ch.state_dir)

    seen: dict[str, object] = {}

    async def _fake_loop(control, *, tui, first):
        seen["tui"] = tui
        return 0

    monkeypatch.setattr(m, "_clear_loop", _fake_loop)
    monkeypatch.setattr(m, "_tui_requested", lambda _a: True)
    monkeypatch.setattr(m, "make_interactive_solicit",
                        lambda *a, **k: (lambda _p: None))

    orig_in, orig_out = sys.stdin.isatty, sys.stdout.isatty
    sys.stdin.isatty = lambda: True   # type: ignore[method-assign]
    sys.stdout.isatty = lambda: True  # type: ignore[method-assign]
    try:
        rc = anyio.run(m._async_cmd_resume, argparse.Namespace(
            token="feat", non_interactive=False, message=None, tui=True))
    finally:
        sys.stdin.isatty = orig_in    # type: ignore[method-assign]
        sys.stdout.isatty = orig_out  # type: ignore[method-assign]

    assert rc == 0
    assert seen["tui"] is True


def test_fresh_session_line_routes_through_clear_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    ch = Change.create(tmp_path, title="demo", change_id="feat", base_branch="main")

    seen: dict[str, object] = {}

    async def _fake_loop(control, *, tui, first):
        seen["tui"] = tui
        return 0

    monkeypatch.setattr(m, "_clear_loop", _fake_loop)
    monkeypatch.setattr(m, "make_interactive_solicit",
                        lambda *a, **k: (lambda _p: None))

    orig_in, orig_out = sys.stdin.isatty, sys.stdout.isatty
    sys.stdin.isatty = lambda: True   # type: ignore[method-assign]
    sys.stdout.isatty = lambda: True  # type: ignore[method-assign]
    try:
        rc = anyio.run(functools.partial(
            m._run_fresh_session, ch,
            args=argparse.Namespace(tui=False, non_interactive=False, message=None),
            seed_opening="hi"))
    finally:
        sys.stdin.isatty = orig_in    # type: ignore[method-assign]
        sys.stdout.isatty = orig_out  # type: ignore[method-assign]

    assert rc == 0
    assert seen["tui"] is False


def test_fresh_session_tui_routes_through_clear_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    ch = Change.create(tmp_path, title="demo", change_id="feat", base_branch="main")

    seen: dict[str, object] = {}

    async def _fake_loop(control, *, tui, first):
        seen["tui"] = tui
        return 0

    monkeypatch.setattr(m, "_clear_loop", _fake_loop)
    monkeypatch.setattr(m, "_tui_requested", lambda _a: True)
    monkeypatch.setattr(m, "make_interactive_solicit",
                        lambda *a, **k: (lambda _p: None))

    orig_in, orig_out = sys.stdin.isatty, sys.stdout.isatty
    sys.stdin.isatty = lambda: True   # type: ignore[method-assign]
    sys.stdout.isatty = lambda: True  # type: ignore[method-assign]
    try:
        rc = anyio.run(functools.partial(
            m._run_fresh_session, ch,
            args=argparse.Namespace(tui=True, non_interactive=False, message=None),
            seed_opening="hi"))
    finally:
        sys.stdin.isatty = orig_in    # type: ignore[method-assign]
        sys.stdout.isatty = orig_out  # type: ignore[method-assign]

    assert rc == 0
    assert seen["tui"] is True


def test_new_tui_routes_through_clear_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)

    seen: dict[str, object] = {}

    async def _fake_loop(control, *, tui, first):
        seen["tui"] = tui
        return 0

    monkeypatch.setattr(m, "_clear_loop", _fake_loop)
    monkeypatch.setattr(m, "_tui_requested", lambda _a: True)

    orig_in, orig_out = sys.stdin.isatty, sys.stdout.isatty
    sys.stdin.isatty = lambda: True   # type: ignore[method-assign]
    sys.stdout.isatty = lambda: True  # type: ignore[method-assign]
    try:
        rc = anyio.run(m._async_cmd_new, argparse.Namespace(
            title=None, opening="add a thing", non_interactive=False,
            message=None, tui=True))
    finally:
        sys.stdin.isatty = orig_in    # type: ignore[method-assign]
        sys.stdout.isatty = orig_out  # type: ignore[method-assign]

    assert rc == 0
    assert seen["tui"] is True        # TUI arm → tui modality


@pytest.mark.anyio
async def test_run_cleared_session_leads_with_header_before_splash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)

    def _fake_splash(*, interactive: bool) -> bool:
        print("SCENE")
        return True

    monkeypatch.setattr(m, "_print_splash_banner", _fake_splash)
    monkeypatch.setattr(m, "should_use_color", lambda *a, **k: False)

    class _FakePrompt:
        def __init__(self, *a: object, **k: object) -> None: ...
        async def ask_async(self, *a: object, **k: object) -> str:
            return "build a parser"

    monkeypatch.setattr(m, "Prompt", _FakePrompt)
    monkeypatch.setattr(m, "make_interactive_solicit",
                        lambda *a, **k: (lambda _p: None))

    async def _fake_client(change: object, **kwargs: object) -> int:
        return 0

    monkeypatch.setattr(m, "run_session_client", _fake_client)

    rc = await m._run_cleared_session(tmp_path, "main", tui=False, leave_state={})

    assert rc == 0
    out = capsys.readouterr().out
    assert " · new" in out                       # the run header (minted id · new)
    assert out.index(" · new") < out.index("SCENE")   # header precedes the splash


def test_resume_interactive_routes_through_clear_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    ch = Change.create(tmp_path, title="demo", change_id="feat", base_branch="main")
    st = State.load(ch.state_dir)
    st.status = "paused"
    st.save(ch.state_dir)

    seen: dict[str, object] = {}

    async def _fake_loop(control, *, tui, first):
        seen["tui"] = tui
        return 0

    monkeypatch.setattr(m, "_clear_loop", _fake_loop)
    monkeypatch.setattr(m, "make_interactive_solicit",
                        lambda *a, **k: (lambda _p: None))

    orig_in, orig_out = sys.stdin.isatty, sys.stdout.isatty
    sys.stdin.isatty = lambda: True   # type: ignore[method-assign]
    sys.stdout.isatty = lambda: True  # type: ignore[method-assign]
    try:
        rc = anyio.run(m._async_cmd_resume, argparse.Namespace(
            token="feat", non_interactive=False, message=None, tui=False))
    finally:
        sys.stdin.isatty = orig_in    # type: ignore[method-assign]
        sys.stdout.isatty = orig_out  # type: ignore[method-assign]

    assert rc == 0
    assert seen["tui"] is False


def test_resume_interactive_leads_with_header_before_splash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    ch = Change.create(tmp_path, title="demo", change_id="feat", base_branch="main")
    st = State.load(ch.state_dir)
    st.status = "paused"
    st.save(ch.state_dir)

    def _fake_splash(*, interactive: bool) -> bool:
        print("SCENE")
        print()
        return True

    monkeypatch.setattr(m, "_print_splash_banner", _fake_splash)
    monkeypatch.setattr(m, "_is_interactive", lambda **k: True)
    monkeypatch.setattr(m, "should_use_color", lambda *a, **k: False)

    async def _fake_loop(control, *, tui, first):
        return 0

    monkeypatch.setattr(m, "_clear_loop", _fake_loop)
    monkeypatch.setattr(m, "make_interactive_solicit",
                        lambda *a, **k: (lambda _p: None))

    orig_in, orig_out = sys.stdin.isatty, sys.stdout.isatty
    sys.stdin.isatty = lambda: True   # type: ignore[method-assign]
    sys.stdout.isatty = lambda: True  # type: ignore[method-assign]
    try:
        rc = anyio.run(m._async_cmd_resume, argparse.Namespace(
            token="feat", non_interactive=False, message=None, tui=False))
    finally:
        sys.stdin.isatty = orig_in    # type: ignore[method-assign]
        sys.stdout.isatty = orig_out  # type: ignore[method-assign]

    assert rc == 0
    out = capsys.readouterr().out
    assert "feat · demo · resuming" in out
    assert out.index("feat · demo · resuming") < out.index("SCENE")
    assert out.endswith("SCENE\n\n")


@pytest.mark.parametrize("splash_shown", [True, False])
def test_resume_interactive_wires_preceded_by_blank_to_splash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, splash_shown: bool,
) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    ch = Change.create(tmp_path, title="demo", change_id="feat", base_branch="main")
    st = State.load(ch.state_dir)
    st.status = "paused"
    st.save(ch.state_dir)

    monkeypatch.setattr(m, "_print_splash_banner", lambda **k: splash_shown)
    monkeypatch.setattr(m, "_is_interactive", lambda **k: True)
    monkeypatch.setattr(m, "should_use_color", lambda *a, **k: False)

    captured: dict[str, object] = {}

    class _CapDriver:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(m, "LiveDriver", _CapDriver)
    monkeypatch.setattr(m, "LiveStatus", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(sig, "InterruptCoordinator", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(m, "make_interactive_solicit", lambda *a, **k: None)

    async def _ok(*a: object, **k: object) -> int:
        return 0

    monkeypatch.setattr(m, "run_session_client", _ok)

    async def _fake_loop(control, *, tui, first):
        return await first({})

    monkeypatch.setattr(m, "_clear_loop", _fake_loop)

    orig_in, orig_out = sys.stdin.isatty, sys.stdout.isatty
    sys.stdin.isatty = lambda: True   # type: ignore[method-assign]
    sys.stdout.isatty = lambda: True  # type: ignore[method-assign]
    try:
        rc = anyio.run(m._async_cmd_resume, argparse.Namespace(
            token="feat", non_interactive=False, message=None, tui=False))
    finally:
        sys.stdin.isatty = orig_in    # type: ignore[method-assign]
        sys.stdout.isatty = orig_out  # type: ignore[method-assign]

    assert rc == 0
    assert captured["preceded_by_blank"] is splash_shown


@pytest.mark.anyio
async def test_run_cleared_session_writes_title_before_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)

    tty = _TTY()
    monkeypatch.setattr(m.sys, "stdout", tty)
    monkeypatch.setattr(m, "_print_splash_banner", lambda **k: False)
    monkeypatch.setattr(m, "should_use_color", lambda *a, **k: False)

    at_prompt: dict[str, str] = {}

    class _CapturePrompt:
        def __init__(self, *a: object, **k: object) -> None: ...

        async def ask_async(self, *a: object, **k: object) -> str:
            at_prompt["value"] = tty.getvalue()
            return "build a parser"

    monkeypatch.setattr(m, "Prompt", _CapturePrompt)
    monkeypatch.setattr(m, "make_interactive_solicit",
                        lambda *a, **k: (lambda _p: None))

    captured: dict[str, object] = {}

    async def _fake_client(change: object, **kwargs: object) -> int:
        captured["change"] = change
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(m, "run_session_client", _fake_client)

    rc = await m._run_cleared_session(tmp_path, "main", tui=False, leave_state={})

    assert rc == 0
    # The working escape for the freshly minted (unnamed) draft is already written.
    assert "\x1b]2;• imp · " in at_prompt["value"]
    title = captured["title"]
    assert title._change_id == captured["change"].id   # one instance threaded through


@pytest.mark.anyio
@pytest.mark.parametrize("exc", [EOFError, KeyboardInterrupt])
async def test_run_cleared_session_cancel_resets_title(
    exc: type[BaseException], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "_print_splash_banner", lambda **k: False)

    reset: list[str] = []

    class _FakeTitle:
        def __init__(self, stream: object, cid: str, name: str) -> None:
            self._change_id = cid
            self._name = name

        def working(self) -> None: ...
        def waiting(self) -> None: ...

        def reset(self) -> None:
            reset.append("reset")

    monkeypatch.setattr(m, "TerminalTitle", _FakeTitle)

    class _CancelPrompt:
        def __init__(self, *a: object, **k: object) -> None: ...

        async def ask_async(self, *a: object, **k: object) -> str:
            raise exc()

    monkeypatch.setattr(m, "Prompt", _CancelPrompt)

    rc = await m._run_cleared_session(tmp_path, "main", tui=False, leave_state={})

    assert rc == 130
    assert reset == ["reset"]


@pytest.mark.anyio
async def test_run_cleared_session_tui_builds_no_title(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "_print_splash_banner", lambda **k: False)

    def _no_title(*a: object, **k: object) -> object:
        raise AssertionError("no TerminalTitle may be constructed on the TUI path")

    monkeypatch.setattr(m, "TerminalTitle", _no_title)

    class _FakePrompt:
        def __init__(self, *a: object, **k: object) -> None: ...

        async def ask_async(self, *a: object, **k: object) -> str:
            return "build a parser"

    monkeypatch.setattr(m, "Prompt", _FakePrompt)

    async def _fake_tui(change: object, **kwargs: object) -> int:
        return 0

    monkeypatch.setattr(m, "_run_tui_session", _fake_tui)

    rc = await m._run_cleared_session(tmp_path, "main", tui=True, leave_state={})
    assert rc == 0
