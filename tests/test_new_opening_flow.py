from __future__ import annotations

import io
from argparse import Namespace
from types import SimpleNamespace

import pytest

import imp.cli.main as main


class _TTY(io.StringIO):
    def isatty(self) -> bool:
        return True


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _new_args() -> Namespace:
    return Namespace(
        description=None, idea=None, workflow=None,
        non_interactive=False,
        title=None, opening=None, message=None, tui=False)


@pytest.fixture
def stub_new(monkeypatch, tmp_path):
    """Stub every boundary collaborator of _async_cmd_new so the test drives
    only its opening-flow control logic. Returns the patched main module."""
    monkeypatch.setattr(main, "install_process_group", lambda: None)
    monkeypatch.setattr(main, "_control_root", lambda: tmp_path)
    monkeypatch.setattr(main.worktree, "check_preconditions", lambda *a, **k: None)
    monkeypatch.setattr(main.worktree, "ensure_imp_ignored", lambda *a, **k: None)
    monkeypatch.setattr(main.worktree, "current_branch", lambda *a, **k: "main")
    monkeypatch.setattr(main, "_preflight_imp_init", lambda **k: None)
    monkeypatch.setattr(main, "_verify_command_nudge", lambda **k: None)
    monkeypatch.setattr(main, "_is_interactive", lambda **k: True)
    monkeypatch.setattr(main, "_print_splash_banner", lambda **k: False)
    monkeypatch.setattr(main, "should_use_color", lambda *a, **k: False)
    monkeypatch.setattr(main, "_term_width", lambda: 80)
    monkeypatch.setattr(main, "_editing_mode", lambda *a, **k: "emacs")
    monkeypatch.setattr(main, "new_change_id", lambda: "z9y8x7")
    monkeypatch.setattr(
        main.Change, "create_draft",
        staticmethod(lambda *a, **k: SimpleNamespace(id="z9y8x7", title="")))
    monkeypatch.setattr(main, "_select_headless", lambda args: (False, False))
    monkeypatch.setattr(main, "_tui_requested", lambda args: False)
    return main


@pytest.mark.anyio
@pytest.mark.parametrize("route", ["interactive", "headless", "tui", "non_interactive"])
async def test_run_header_emitted_exactly_once(stub_new, monkeypatch, route) -> None:
    m = stub_new
    calls = {"header": 0}

    def _count_header(*a: object, **k: object) -> None:
        calls["header"] += 1

    monkeypatch.setattr(m, "_print_run_header", _count_header)

    class _FakePrompt:
        def __init__(self, **kwargs: object) -> None:
            pass

        async def ask_async(self, **kwargs: object) -> str:
            return "do the thing"

    monkeypatch.setattr(m, "Prompt", _FakePrompt)

    async def _ok(*a: object, **k: object) -> int:
        return 0

    monkeypatch.setattr(m, "run_session_client", _ok)
    monkeypatch.setattr(m, "make_interactive_solicit", lambda *a, **k: None)
    monkeypatch.setattr(m, "make_headless_solicit", lambda *a, **k: None)
    monkeypatch.setattr(m, "LiveStatus", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(m, "LiveDriver", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(m, "_run_tui_session", _ok)

    if route == "headless":
        monkeypatch.setattr(m, "_select_headless", lambda args: (True, False))
    elif route == "tui":
        monkeypatch.setattr(m, "_tui_requested", lambda args: True)
    elif route == "non_interactive":
        monkeypatch.setattr(m, "_is_interactive", lambda **k: False)

    rc = await m._async_cmd_new(_new_args(), leave_mode="new")

    assert rc == 0
    # The prompt path prints the header; every dispatch branch (interactive,
    # headless, tui) is guarded by `if not header_printed`, so it stays at 1.
    assert calls["header"] == 1


@pytest.mark.anyio
async def test_cancel_at_opening_prompt_discards_the_minted_draft(
    stub_new, monkeypatch,
) -> None:
    m = stub_new
    monkeypatch.setattr(m, "_print_run_header", lambda *a, **k: None)
    discarded: dict[str, str] = {}

    class _CancelPrompt:
        def __init__(self, **kwargs: object) -> None:
            pass

        async def ask_async(self, **kwargs: object) -> str:
            raise KeyboardInterrupt

    monkeypatch.setattr(m, "Prompt", _CancelPrompt)
    monkeypatch.setattr(
        m, "discard_conversation",
        lambda change: discarded.__setitem__("id", change.id))

    rc = await m._async_cmd_new(_new_args(), leave_mode="new")

    assert rc == 130
    assert discarded["id"] == "z9y8x7"   # the just-minted draft was torn down


@pytest.mark.anyio
async def test_seeded_interactive_leads_with_header_before_splash(
    stub_new, monkeypatch, capsys,
) -> None:
    m = stub_new

    def _fake_splash(*, interactive, lead_blank=True, stream=None):
        if interactive:
            print("SCENE")
            return True
        return False

    monkeypatch.setattr(m, "_print_splash_banner", _fake_splash)

    class _FakePrompt:
        def __init__(self, **kwargs: object) -> None:
            pass

        async def ask_async(self, **kwargs: object) -> str:
            return "unused"

    monkeypatch.setattr(m, "Prompt", _FakePrompt)

    async def _ok(*a: object, **k: object) -> int:
        return 0

    monkeypatch.setattr(m, "run_session_client", _ok)
    monkeypatch.setattr(m, "make_interactive_solicit", lambda *a, **k: None)
    monkeypatch.setattr(m, "LiveStatus", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(m, "LiveDriver", lambda *a, **k: SimpleNamespace())

    args = _new_args()
    args.opening = "build a parser"
    rc = await m._async_cmd_new(args, leave_mode="new")

    assert rc == 0
    out = capsys.readouterr().out
    assert "z9y8x7 · new\n\nSCENE\n" in out
    assert out.index("z9y8x7") < out.index("SCENE")


@pytest.mark.anyio
async def test_title_written_before_opening_prompt(stub_new, monkeypatch) -> None:
    m = stub_new
    tty = _TTY()
    monkeypatch.setattr(m.sys, "stdout", tty)        # real TerminalTitle writes here
    monkeypatch.setattr(m, "_print_run_header", lambda *a, **k: None)

    at_prompt: dict[str, str] = {}

    class _CapturePrompt:
        def __init__(self, **kwargs: object) -> None: ...

        async def ask_async(self, **kwargs: object) -> str:
            at_prompt["value"] = tty.getvalue()      # stdout contents when the prompt blocks
            return "do the thing"

    monkeypatch.setattr(m, "Prompt", _CapturePrompt)

    captured: dict[str, object] = {}

    async def _capture_client(change: object, **kwargs: object) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(m, "run_session_client", _capture_client)
    monkeypatch.setattr(m, "make_interactive_solicit", lambda *a, **k: None)
    monkeypatch.setattr(m, "LiveStatus", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(m, "LiveDriver", lambda *a, **k: SimpleNamespace())

    rc = await m._async_cmd_new(_new_args(), leave_mode="new")

    assert rc == 0
    # The working escape (unnamed draft → "• imp · <id>") is already on stdout.
    assert "\x1b]2;• imp · z9y8x7\x07" in at_prompt["value"]
    # The same instance is threaded into run_session_client.
    assert captured["title"]._change_id == "z9y8x7"


@pytest.mark.anyio
@pytest.mark.parametrize("exc", [EOFError, KeyboardInterrupt])
async def test_cancel_at_opening_prompt_resets_title(stub_new, monkeypatch, exc) -> None:
    m = stub_new
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
    monkeypatch.setattr(m, "_print_run_header", lambda *a, **k: None)
    monkeypatch.setattr(m, "discard_conversation", lambda change: None)

    class _CancelPrompt:
        def __init__(self, **kwargs: object) -> None: ...

        async def ask_async(self, **kwargs: object) -> str:
            raise exc()

    monkeypatch.setattr(m, "Prompt", _CancelPrompt)

    rc = await m._async_cmd_new(_new_args(), leave_mode="new")

    assert rc == 130
    assert reset == ["reset"]


@pytest.mark.anyio
async def test_tui_path_builds_no_title(stub_new, monkeypatch) -> None:
    m = stub_new
    monkeypatch.setattr(m, "_tui_requested", lambda args: True)

    def _no_title(*a: object, **k: object) -> object:
        raise AssertionError("no TerminalTitle may be constructed on the TUI path")

    monkeypatch.setattr(m, "TerminalTitle", _no_title)
    monkeypatch.setattr(m, "_print_run_header", lambda *a, **k: None)
    monkeypatch.setattr(m, "_print_opening", lambda *a, **k: None)

    class _FakePrompt:
        def __init__(self, **kwargs: object) -> None: ...

        async def ask_async(self, **kwargs: object) -> str:
            return "do the thing"

    monkeypatch.setattr(m, "Prompt", _FakePrompt)

    async def _ok(*a: object, **k: object) -> int:
        return 0

    monkeypatch.setattr(m, "_run_tui_session", _ok)

    rc = await m._async_cmd_new(_new_args(), leave_mode="new")
    assert rc == 0


@pytest.mark.anyio
async def test_headless_path_threads_title(stub_new, monkeypatch) -> None:
    m = stub_new
    monkeypatch.setattr(m, "_select_headless", lambda args: (True, False))
    monkeypatch.setattr(m, "_print_run_header", lambda *a, **k: None)

    class _FakePrompt:
        def __init__(self, **kwargs: object) -> None: ...

        async def ask_async(self, **kwargs: object) -> str:
            return "do the thing"

    monkeypatch.setattr(m, "Prompt", _FakePrompt)

    captured: dict[str, object] = {}

    async def _capture_client(change: object, **kwargs: object) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(m, "run_session_client", _capture_client)
    monkeypatch.setattr(m, "make_headless_solicit", lambda *a, **k: None)
    monkeypatch.setattr(m, "LiveStatus", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(m, "LiveDriver", lambda *a, **k: SimpleNamespace())

    rc = await m._async_cmd_new(_new_args(), leave_mode="new")

    assert rc == 0
    assert captured["title"]._change_id == "z9y8x7"
