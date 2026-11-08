from __future__ import annotations

import asyncio
import contextlib
import io
import os
import signal
from argparse import Namespace
from types import SimpleNamespace

import pytest

import imp.cli.main as main
from tests.async_wait import HANG_BACKSTOP


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
        title=None, opening=None, message=None)


@pytest.fixture
def stub_new(monkeypatch, tmp_path):
    """Stub every boundary collaborator of _async_cmd_new so the test drives
    only its opening-flow control logic. Returns the patched main module."""
    monkeypatch.setattr(main, "_control_root", lambda: tmp_path)
    monkeypatch.setattr("imp.engine.worktree.check_preconditions", lambda *a, **k: None)
    monkeypatch.setattr("imp.engine.worktree.ensure_imp_ignored", lambda *a, **k: None)
    monkeypatch.setattr(main, "_resolve_new_base", lambda *a, **k: "main")
    monkeypatch.setattr(main, "_preflight_imp_init", lambda **k: None)
    monkeypatch.setattr(main, "_is_interactive", lambda **k: True)
    monkeypatch.setattr(main, "_print_splash_banner", lambda **k: False)
    monkeypatch.setattr(main, "should_use_color", lambda *a, **k: False)
    monkeypatch.setattr(main, "_term_width", lambda: 80)
    monkeypatch.setattr(main, "_editing_mode", lambda *a, **k: "emacs")
    monkeypatch.setattr(
        "imp.engine.quest_id.new_quest_id", lambda existing_stems=frozenset(): "z9y8x7")
    monkeypatch.setattr(
        "imp.engine.quest.Quest.create_draft",
        staticmethod(lambda *a, **k: SimpleNamespace(
            id="z9y8x7", title="", workflow_dir=tmp_path, seed_opening=None,
            control_root=tmp_path,
            set_seed_opening=lambda text: None)))
    monkeypatch.setattr(main, "_select_headless", lambda args: (False, False))
    return main


@pytest.mark.anyio
@pytest.mark.parametrize(
    "opening_arg,reply,expected",
    [("build a parser", None, "build a parser"),   # positional: argv intent, no prompt
     (None, "wire up json", "wire up json")],       # interactive: prompt reply
)
async def test_new_persists_finalized_opening_intent(
    stub_new, monkeypatch, tmp_path, opening_arg, reply, expected,
) -> None:
    m = stub_new
    monkeypatch.setattr(m, "_print_run_header", lambda *a, **k: None)
    monkeypatch.setattr(m, "_print_opening", lambda *a, **k: None)

    recorded: dict[str, object] = {}

    def _make_draft(*a: object, **k: object) -> SimpleNamespace:
        return SimpleNamespace(
            id="z9y8x7", title="", workflow_dir=tmp_path, seed_opening=None,
            control_root=tmp_path,
            set_seed_opening=lambda text: recorded.__setitem__("text", text))

    monkeypatch.setattr("imp.engine.quest.Quest.create_draft", staticmethod(_make_draft))

    class _FakePrompt:
        def __init__(self, **kwargs: object) -> None: ...

        async def ask_async(self, **kwargs: object) -> str:
            return reply

    monkeypatch.setattr(m, "Prompt", _FakePrompt)

    async def _ok(*a: object, **k: object) -> int:
        return 0

    monkeypatch.setattr("imp.cli.session_run.run_session_client", _ok)
    monkeypatch.setattr("imp.cli.session_run.make_interactive_solicit", lambda *a, **k: None)
    monkeypatch.setattr("imp.cli.live_status.LiveStatus", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr("imp.cli.live_driver.LiveDriver", lambda *a, **k: SimpleNamespace())

    args = _new_args()
    args.opening = opening_arg
    rc = await m._async_cmd_new(args, leave_mode="new")

    assert rc == 0
    assert "text" in recorded          # set_seed_opening was actually called...
    assert recorded["text"] == expected  # ...with the finalized intent (None when unseeded)


@pytest.mark.anyio
@pytest.mark.parametrize("route", ["interactive", "headless", "non_interactive"])
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

    monkeypatch.setattr("imp.cli.session_run.run_session_client", _ok)
    monkeypatch.setattr("imp.cli.session_run.make_interactive_solicit", lambda *a, **k: None)
    monkeypatch.setattr("imp.cli.session_run.make_headless_solicit", lambda *a, **k: None)
    monkeypatch.setattr("imp.cli.live_status.LiveStatus", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr("imp.cli.live_driver.LiveDriver", lambda *a, **k: SimpleNamespace())

    if route == "headless":
        monkeypatch.setattr(m, "_select_headless", lambda args: (True, False))
    elif route == "non_interactive":
        monkeypatch.setattr(m, "_is_interactive", lambda **k: False)

    rc = await m._async_cmd_new(_new_args(), leave_mode="new")

    assert rc == 0
    # The prompt path prints the header; every dispatch branch (interactive,
    # headless) is guarded by `if not header_printed`, so it stays at 1.
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
        "imp.engine.abandon.discard_conversation",
        lambda quest: discarded.__setitem__("id", quest.id))

    rc = await m._async_cmd_new(_new_args(), leave_mode="new")

    assert rc == 130
    assert discarded["id"] == "z9y8x7"   # the just-minted draft was torn down


@pytest.mark.anyio
@pytest.mark.parametrize("exc", [EOFError, KeyboardInterrupt])
async def test_cancel_at_opening_prompt_emits_discarded_spine(
    stub_new, monkeypatch, capsys, exc,
) -> None:
    # No exchange at the opening prompt (Ctrl-D/Ctrl-C) -> the discarded spine
    # `<id> · (unnamed) · discarded`, one trailing blank, no caret line, exit 130.
    # stub_new forces should_use_color -> False, so the spine renders plain.
    m = stub_new
    monkeypatch.setattr(m, "_print_run_header", lambda *a, **k: None)
    monkeypatch.setattr("imp.engine.abandon.discard_conversation", lambda quest: None)

    class _CancelPrompt:
        def __init__(self, **kwargs: object) -> None: ...

        async def ask_async(self, **kwargs: object) -> str:
            raise exc()

    monkeypatch.setattr(m, "Prompt", _CancelPrompt)

    rc = await m._async_cmd_new(_new_args(), leave_mode="new")

    assert rc == 130
    out = capsys.readouterr().out
    assert out.endswith("z9y8x7 · (unnamed) · discarded\n\n")
    assert not out.endswith("discarded\n\n\n")   # exactly one trailing blank
    assert "> " not in out                       # no bare caret line
    assert "cancelled" not in out


@pytest.mark.anyio
@pytest.mark.parametrize("leave_mode", ["new", "chat"])
@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGHUP], ids=["sigterm", "sighup"])
async def test_leave_signal_at_opening_prompt_discards_like_ctrl_d(
    stub_new, monkeypatch, capsys, signal_sentinels, leave_mode, sig,
) -> None:
    # Same as Ctrl-D: discarded spine + one blank, draft torn down, rc 130, no pause block.
    m = stub_new
    monkeypatch.delenv("IMP_SESSION_SOCKET", raising=False)
    monkeypatch.setattr(m, "_print_run_header", lambda *a, **k: None)
    discarded: list[str] = []
    monkeypatch.setattr(
        "imp.engine.abandon.discard_conversation", lambda quest: discarded.append(quest.id))

    class _SignalledPrompt:
        def __init__(self, **kwargs: object) -> None: ...

        async def ask_async(self, **kwargs: object) -> str:
            os.kill(os.getpid(), sig)
            await asyncio.Event().wait()
            raise AssertionError("the leave signal must cancel the blocked prompt")

    monkeypatch.setattr(m, "Prompt", _SignalledPrompt)

    async with asyncio.timeout(HANG_BACKSTOP):
        rc = await m._async_cmd_new(_new_args(), leave_mode=leave_mode)

    assert rc == 130
    assert discarded == ["z9y8x7"]
    out = capsys.readouterr().out
    assert out.endswith("z9y8x7 · (unnamed) · discarded\n\n")
    assert not out.endswith("discarded\n\n\n")
    assert "paused" not in out
    assert "SIGTERM" not in out and "SIGHUP" not in out


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

    monkeypatch.setattr("imp.cli.session_run.run_session_client", _ok)
    monkeypatch.setattr("imp.cli.session_run.make_interactive_solicit", lambda *a, **k: None)
    monkeypatch.setattr("imp.cli.live_status.LiveStatus", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr("imp.cli.live_driver.LiveDriver", lambda *a, **k: SimpleNamespace())

    args = _new_args()
    args.opening = "build a parser"
    rc = await m._async_cmd_new(args, leave_mode="new")

    assert rc == 0
    out = capsys.readouterr().out
    assert "z9y8x7 · new\n\nSCENE\n" in out
    assert out.index("z9y8x7") < out.index("SCENE")


@pytest.mark.anyio
async def test_title_written_before_opening_prompt(stub_new, monkeypatch, tmp_path) -> None:
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

    async def _capture_client(quest: object, **kwargs: object) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr("imp.cli.session_run.run_session_client", _capture_client)
    monkeypatch.setattr("imp.cli.session_run.make_interactive_solicit", lambda *a, **k: None)
    monkeypatch.setattr("imp.cli.live_status.LiveStatus", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr("imp.cli.live_driver.LiveDriver", lambda *a, **k: SimpleNamespace())

    rc = await m._async_cmd_new(_new_args(), leave_mode="new")

    assert rc == 0
    # The idle escape (unnamed draft → "ѱ <id>") is already on stdout:
    # the opening prompt is blocked on the user, so it shows the idle glyph.
    assert f"\x1b]2;ѱ {tmp_path.name} · z9y8x7\x07" in at_prompt["value"]
    # The same instance is threaded into run_session_client.
    assert captured["title"]._quest_id == "z9y8x7"


@pytest.mark.anyio
@pytest.mark.parametrize("exc", [EOFError, KeyboardInterrupt])
async def test_cancel_at_opening_prompt_resets_title(stub_new, monkeypatch, exc) -> None:
    m = stub_new
    reset: list[str] = []

    class _FakeTitle:
        def __init__(
            self, stream: object, cid: str, name: str, initial: str = "busy",
            *, project: str = "",
        ) -> None:
            self._quest_id = cid
            self._name = name

        def busy(self) -> None: ...
        def idle(self) -> None: ...

        def reset(self) -> None:
            reset.append("reset")

    monkeypatch.setattr("imp.cli.terminal_title.TerminalTitle", _FakeTitle)
    monkeypatch.setattr(m, "_print_run_header", lambda *a, **k: None)
    monkeypatch.setattr("imp.engine.abandon.discard_conversation", lambda quest: None)

    class _CancelPrompt:
        def __init__(self, **kwargs: object) -> None: ...

        async def ask_async(self, **kwargs: object) -> str:
            raise exc()

    monkeypatch.setattr(m, "Prompt", _CancelPrompt)

    rc = await m._async_cmd_new(_new_args(), leave_mode="new")

    assert rc == 130
    assert reset == ["reset"]


@pytest.mark.anyio
@pytest.mark.parametrize("selected", [(True, False), (True, True), (False, False)],
                         ids=["headless", "auto-headless", "interactive"])
async def test_new_seeds_a_blank_tail(stub_new, monkeypatch, selected) -> None:
    m = stub_new
    monkeypatch.setattr(m, "_select_headless", lambda args: selected)
    monkeypatch.setattr(m, "_print_run_header", lambda *a, **k: None)
    monkeypatch.setattr(m, "_print_opening", lambda *a, **k: None)

    driver_kwargs: dict[str, object] = {}

    def _driver(**kwargs: object) -> SimpleNamespace:
        driver_kwargs.update(kwargs)
        return SimpleNamespace()

    async def _ok(*a: object, **k: object) -> int:
        return 0

    monkeypatch.setattr("imp.cli.session_run.run_session_client", _ok)
    monkeypatch.setattr("imp.cli.session_run.make_interactive_solicit", lambda *a, **k: None)
    monkeypatch.setattr("imp.cli.session_run.make_headless_solicit", lambda *a, **k: None)
    monkeypatch.setattr("imp.cli.live_status.LiveStatus", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr("imp.cli.live_driver.LiveDriver", _driver)

    args = _new_args()
    args.opening = "do the thing"
    assert await m._async_cmd_new(args, leave_mode="new") == 0
    assert driver_kwargs["preceded_by_blank"] is True


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

    async def _capture_client(quest: object, **kwargs: object) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr("imp.cli.session_run.run_session_client", _capture_client)
    monkeypatch.setattr("imp.cli.session_run.make_headless_solicit", lambda *a, **k: None)
    monkeypatch.setattr("imp.cli.live_status.LiveStatus", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr("imp.cli.live_driver.LiveDriver", lambda *a, **k: SimpleNamespace())

    rc = await m._async_cmd_new(_new_args(), leave_mode="new")

    assert rc == 0
    assert captured["title"]._quest_id == "z9y8x7"


@pytest.mark.anyio
async def test_opening_prompt_wrapped_in_prompt_signal_guard(stub_new, monkeypatch) -> None:
    """The opening prompt is awaited inside prompt_signal_guard, catching early signals."""
    m = stub_new
    monkeypatch.setattr(m, "_print_run_header", lambda *a, **k: None)

    entered = {"v": False}

    @contextlib.asynccontextmanager
    async def _fake_guard():
        entered["v"] = True
        yield

    monkeypatch.setattr("imp.cli.signals.prompt_signal_guard", _fake_guard)

    prompt_ran = {"v": False}

    class _FakePrompt:
        def __init__(self, **kwargs: object) -> None: ...

        async def ask_async(self, **kwargs: object) -> str:
            assert entered["v"] is True   # guard active while the prompt blocks
            prompt_ran["v"] = True
            return "do the thing"

    monkeypatch.setattr(m, "Prompt", _FakePrompt)

    async def _ok(*a: object, **k: object) -> int:
        return 0

    monkeypatch.setattr("imp.cli.session_run.run_session_client", _ok)
    monkeypatch.setattr("imp.cli.session_run.make_interactive_solicit", lambda *a, **k: None)
    monkeypatch.setattr("imp.cli.live_status.LiveStatus", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr("imp.cli.live_driver.LiveDriver", lambda *a, **k: SimpleNamespace())

    rc = await m._async_cmd_new(_new_args(), leave_mode="new")

    assert rc == 0
    assert prompt_ran["v"] is True
    assert entered["v"] is True


@pytest.mark.anyio
async def test_new_run_now_forwards_depends_on(
        stub_new, monkeypatch, tmp_path) -> None:
    m = stub_new
    monkeypatch.delenv("IMP_SESSION_SOCKET", raising=False)
    monkeypatch.setattr(m, "_print_opening", lambda *a, **k: None)
    monkeypatch.setattr(m, "_print_run_header", lambda *a, **k: None)

    captured: dict[str, object] = {}

    def _cap(*a: object, **k: object) -> SimpleNamespace:
        captured.update(k)
        return SimpleNamespace(
            id="z9y8x7", title="", workflow_dir=tmp_path, seed_opening=None,
            control_root=tmp_path,
            set_seed_opening=lambda text: None)

    monkeypatch.setattr("imp.engine.quest.Quest.create_draft", staticmethod(_cap))

    async def _ok(*a: object, **k: object) -> int:
        return 0

    monkeypatch.setattr("imp.cli.session_run.run_session_client", _ok)
    monkeypatch.setattr("imp.cli.session_run.make_interactive_solicit", lambda *a, **k: None)
    monkeypatch.setattr("imp.cli.live_status.LiveStatus", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr("imp.cli.live_driver.LiveDriver", lambda *a, **k: SimpleNamespace())

    args = Namespace(
        description=None, idea=None, workflow=None, non_interactive=False,
        title=None, opening="build it", message=None, base=None,
        background=False, depends_on=["dep9"])
    rc = await m._async_cmd_new(args, leave_mode="new")
    assert rc == 0
    assert captured["depends_on"] == ["dep9"]


@pytest.mark.anyio
async def test_new_background_delegates_to_apply_new(
        stub_new, monkeypatch, capsys) -> None:
    m = stub_new
    monkeypatch.delenv("IMP_SESSION_SOCKET", raising=False)
    calls: dict[str, object] = {}

    def _fake_apply_new(control: object, **kw: object) -> tuple[int, str]:
        calls.update(kw)
        return 0, "Started quest z9y8x7 (background)"

    monkeypatch.setattr(m, "apply_new", _fake_apply_new)

    args = Namespace(
        description=None, idea=None, workflow=None, non_interactive=False,
        title="T", opening="brief", message=None, base=None,
        background=True, depends_on=["d1"])
    rc = await m._async_cmd_new(args, leave_mode="new")
    assert rc == 0
    assert calls["background"] is True
    assert calls["depends_on"] == ["d1"]
    assert calls.get("idea") is None
    out = capsys.readouterr()
    assert "Started quest z9y8x7" in out.out


@pytest.mark.anyio
async def test_new_background_error_prints_to_stderr(
        stub_new, monkeypatch, capsys) -> None:
    m = stub_new
    monkeypatch.delenv("IMP_SESSION_SOCKET", raising=False)

    def _fail_apply_new(control: object, **kw: object) -> tuple[int, str]:
        return 2, "imp: boom"

    monkeypatch.setattr(m, "apply_new", _fail_apply_new)

    args = Namespace(
        description=None, idea=None, workflow=None, non_interactive=False,
        title="T", opening="brief", message=None, base=None,
        background=True, depends_on=None)
    rc = await m._async_cmd_new(args, leave_mode="new")
    assert rc == 2
    err = capsys.readouterr()
    assert "imp: boom" in err.err
    assert "imp: boom" not in err.out


@pytest.mark.anyio
@pytest.mark.parametrize("leave_mode,expected", [("chat", "chat"), ("new", "new")])
async def test_create_draft_receives_create_mode_from_leave_mode(
    stub_new, monkeypatch, tmp_path, leave_mode, expected,
) -> None:
    m = stub_new
    monkeypatch.delenv("IMP_SESSION_SOCKET", raising=False)
    monkeypatch.setattr(m, "_print_opening", lambda *a, **k: None)
    monkeypatch.setattr(m, "_print_run_header", lambda *a, **k: None)

    captured: dict[str, object] = {}

    def _cap(*a: object, **k: object) -> SimpleNamespace:
        captured.update(k)
        return SimpleNamespace(
            id="z9y8x7", title="", workflow_dir=tmp_path, seed_opening=None,
            control_root=tmp_path,
            set_seed_opening=lambda text: None)

    monkeypatch.setattr("imp.engine.quest.Quest.create_draft", staticmethod(_cap))

    async def _ok(*a: object, **k: object) -> int:
        return 0

    monkeypatch.setattr("imp.cli.session_run.run_session_client", _ok)
    monkeypatch.setattr("imp.cli.session_run.make_interactive_solicit", lambda *a, **k: None)
    monkeypatch.setattr("imp.cli.live_status.LiveStatus", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr("imp.cli.live_driver.LiveDriver", lambda *a, **k: SimpleNamespace())

    args = Namespace(
        description=None, idea=None, workflow=None, non_interactive=False,
        title=None, opening="build it", message=None, base=None,
        background=False, depends_on=None)
    rc = await m._async_cmd_new(args, leave_mode=leave_mode)

    assert rc == 0
    assert captured["create_mode"] == expected


@pytest.mark.anyio
@pytest.mark.parametrize("leave_mode", ["chat", "new"])
async def test_headless_new_threads_leave_mode_to_run_session_client(
    stub_new, monkeypatch, leave_mode,
) -> None:
    m = stub_new
    monkeypatch.setattr(m, "_select_headless", lambda args: (True, False))
    monkeypatch.setattr(m, "_print_run_header", lambda *a, **k: None)
    monkeypatch.setattr("imp.cli.session_run.make_headless_solicit", lambda *a, **k: None)
    monkeypatch.setattr("imp.cli.live_status.LiveStatus", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr("imp.cli.live_driver.LiveDriver", lambda *a, **k: SimpleNamespace())

    captured: dict[str, object] = {}

    async def _cap(*a: object, **k: object) -> int:
        captured.update(k)
        return 0

    monkeypatch.setattr("imp.cli.session_run.run_session_client", _cap)

    args = Namespace(
        description=None, idea=None, workflow=None, non_interactive=True,
        title=None, opening="build it", message=None, base=None,
        background=False, depends_on=None)
    rc = await m._async_cmd_new(args, leave_mode=leave_mode)

    assert rc == 0
    assert captured["leave_mode"] == leave_mode


@pytest.mark.anyio
async def test_new_says_nothing_about_test_commands(
    stub_new, monkeypatch, capsys, tmp_path,
) -> None:
    """`imp new` no longer interrogates the user about test_cmd_fast — the front
    door raises it instead. Detectable command + unset config + no message."""
    m = stub_new
    monkeypatch.setenv("IMP_HOME", str(tmp_path / "imphome"))
    (tmp_path / "imp").mkdir(exist_ok=True)
    (tmp_path / "imp" / "config.yaml").write_text("default-workflow: imp\n")
    (tmp_path / "Makefile").write_text("test:\n\techo hi\n")
    monkeypatch.setattr(m, "_print_run_header", lambda *a, **k: None)
    monkeypatch.setattr(m, "_print_opening", lambda *a, **k: None)
    monkeypatch.setattr(m, "_is_interactive", lambda **k: False)

    async def _ok(*a: object, **k: object) -> int:
        return 0

    monkeypatch.setattr("imp.cli.session_run.run_session_client", _ok)
    monkeypatch.setattr("imp.cli.session_run.make_interactive_solicit", lambda *a, **k: None)
    monkeypatch.setattr("imp.cli.session_run.make_headless_solicit", lambda *a, **k: None)
    monkeypatch.setattr("imp.cli.live_status.LiveStatus", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr("imp.cli.live_driver.LiveDriver", lambda *a, **k: SimpleNamespace())

    args = _new_args()
    args.non_interactive = True
    args.opening = "build a parser"

    rc = await m._async_cmd_new(args, leave_mode="new")

    assert rc == 0
    captured = capsys.readouterr()
    assert "test_cmd_fast" not in captured.out + captured.err
    assert "Detected `" not in captured.out + captured.err
