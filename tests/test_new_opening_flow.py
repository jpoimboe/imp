from __future__ import annotations

from argparse import Namespace
from types import SimpleNamespace

import pytest

import imp.cli.main as main


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
