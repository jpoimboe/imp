from __future__ import annotations

import argparse
from pathlib import Path
from types import SimpleNamespace

import pytest

import imp.cli.main as main
from tests.conftest import _seed_control


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _QueuePrompt:
    """Returns queued lines in order from ask_async (accepts the interceptor's kwargs)."""

    def __init__(self, lines: list[str]) -> None:
        self._lines = list(lines)

    async def ask_async(self, prompt_text: str | None = None, **kwargs: object) -> str:
        return self._lines.pop(0)


class _FakeChange:
    """Minimal Change stand-in for the opening-prompt slash path: carries the id/
    title _print_opening needs plus the workflow_dir/event_log handle_slash_line
    touches."""

    def __init__(self, tmp_path: Path) -> None:
        self.id = "z9y8x7"
        self.title = ""
        self.control_root = tmp_path
        self.workflow_dir = tmp_path / ".imp" / "change" / "workflow"
        (self.workflow_dir / "skills" / "user").mkdir(parents=True, exist_ok=True)
        self._events = tmp_path / "events.jsonl"

    def event_log(self):
        from imp.engine.state import JsonlEventLog
        return JsonlEventLog(self._events)


def _fake_change(tmp_path: Path) -> _FakeChange:
    return _FakeChange(tmp_path)


def _fake_intent_line(*, model: str, reply: str):
    """Stand-in for _read_intent_line: populates the passed model_sink and returns
    the reply, mirroring the new signature."""

    async def _f(prompt, change, *, color, width, model_sink=None):
        if model_sink is not None:
            model_sink["model"] = model
        return reply

    return _f


@pytest.mark.anyio
async def test_opening_prompt_intercepts_help_then_returns_intent(
    capsys: pytest.CaptureFixture[str],
) -> None:
    change = SimpleNamespace(id="z9y8x7", title="")
    reply = await main._read_opening_message(
        change, leave_mode="new", banner_shown=False, color=False,
        width=80, editing_mode="emacs",
        _prompt_factory=lambda: _QueuePrompt(["/help", "add oauth"]))
    # /help was handled client-side and the loop kept reading; only the real
    # message becomes the intent (the old direct-read path returned "/help").
    assert reply == "add oauth"
    out = capsys.readouterr().out
    assert "Available slash commands" in out
    assert "Keybindings" in out


@pytest.mark.anyio
async def test_opening_prompt_session_verb_reprompts(
    capsys: pytest.CaptureFixture[str],
) -> None:
    change = SimpleNamespace(id="z9y8x7", title="")
    reply = await main._read_opening_message(
        change, leave_mode="new", banner_shown=False, color=False,
        width=80, editing_mode="emacs",
        _prompt_factory=lambda: _QueuePrompt(["/clear", "real intent"]))
    assert reply == "real intent"        # /clear did not become the draft intent
    out = capsys.readouterr().out
    assert "/clear" in out               # a note explained why it was ignored


@pytest.mark.anyio
async def test_opening_prompt_leading_slash_path_submits_as_input(
    tmp_path: Path,
) -> None:
    # A pasted absolute path is not a command — it must submit verbatim.
    change = SimpleNamespace(id="z9y8x7", title="", workflow_dir=tmp_path)
    reply = await main._read_opening_message(
        change, leave_mode="new", banner_shown=False, color=False,
        width=80, editing_mode="emacs",
        _prompt_factory=lambda: _QueuePrompt(["/home/user/foo.py"]))
    assert reply == "/home/user/foo.py"


@pytest.mark.anyio
async def test_read_intent_line_captures_model_and_reprompts(
    tmp_path, monkeypatch,
) -> None:
    import imp.cli.slash as slash

    prompt = _QueuePrompt(["/model anthropic:claude-fable-5", "brainstorm this"])
    change = _fake_change(tmp_path)
    monkeypatch.setattr(slash, "validate_model_arg", lambda arg, *, change: None)
    sink: dict[str, str] = {}

    line = await main._read_intent_line(
        prompt, change, color=False, width=80, model_sink=sink)

    assert line == "brainstorm this"
    assert sink["model"] == "anthropic:claude-fable-5"


@pytest.mark.anyio
async def test_read_opening_message_threads_sink(tmp_path, monkeypatch) -> None:
    import imp.cli.slash as slash

    monkeypatch.setattr(slash, "validate_model_arg", lambda arg, *, change: None)
    change = _fake_change(tmp_path)
    sink: dict[str, str] = {}

    reply = await main._read_opening_message(
        change, leave_mode="new", banner_shown=False, color=False, width=80,
        editing_mode="emacs", model_sink=sink,
        _prompt_factory=lambda: _QueuePrompt(
            ["/model anthropic:claude-fable-5", "brainstorm this"]))

    assert reply == "brainstorm this"
    assert sink["model"] == "anthropic:claude-fable-5"


@pytest.mark.anyio
async def test_async_cmd_new_threads_model_to_run_session_client(
    tmp_path, monkeypatch,
) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)

    async def _fake_open(change, *, model_sink=None, **kw):
        if model_sink is not None:
            model_sink["model"] = "anthropic:claude-fable-5"
        return "brainstorm this"

    captured: dict[str, object] = {}

    async def _fake_rsc(change, **kw):
        captured.update(kw)
        return 0

    monkeypatch.setattr(main, "_read_opening_message", _fake_open)
    monkeypatch.setattr(main, "run_session_client", _fake_rsc)
    monkeypatch.setattr(main, "_should_prompt_opening", lambda **kw: True)
    monkeypatch.setattr(main, "_select_headless", lambda args: (False, False))

    rc = await main._async_cmd_new(argparse.Namespace(
        base=None, workflow=None, idea=None, title=None,
        non_interactive=False, autonomous=False, message=None,
        opening=None, depends_on=None))

    assert rc == 0
    assert captured["model"] == "anthropic:claude-fable-5"


@pytest.mark.anyio
async def test_async_cmd_new_threads_model_to_headless_session_client(
    tmp_path, monkeypatch,
) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)

    async def _fake_open(change, *, model_sink=None, **kw):
        if model_sink is not None:
            model_sink["model"] = "anthropic:claude-fable-5"
        return "brainstorm this"

    captured: dict[str, object] = {}

    async def _fake_rsc(change, **kw):
        captured.update(kw)
        return 0

    monkeypatch.setattr(main, "_read_opening_message", _fake_open)
    monkeypatch.setattr(main, "run_session_client", _fake_rsc)
    monkeypatch.setattr(main, "_should_prompt_opening", lambda **kw: True)
    monkeypatch.setattr(main, "_select_headless", lambda args: (True, False))

    rc = await main._async_cmd_new(argparse.Namespace(
        base=None, workflow=None, idea=None, title=None,
        non_interactive=False, autonomous=False, message=None,
        opening=None, depends_on=None))

    assert rc == 0
    assert captured["model"] == "anthropic:claude-fable-5"


@pytest.mark.anyio
async def test_cleared_session_threads_model(tmp_path, monkeypatch) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        main, "_read_intent_line",
        _fake_intent_line(model="anthropic:claude-fable-5", reply="brainstorm"))
    monkeypatch.setattr(main, "_print_splash_banner", lambda *, interactive: False)
    monkeypatch.setattr(main, "make_interactive_solicit",
                        lambda *a, **k: (lambda _p: None))

    class _StubPrompt:
        def __init__(self, *a: object, **k: object) -> None: ...
        async def ask_async(self, *a: object, **k: object) -> str:
            return "brainstorm"

    monkeypatch.setattr(main, "Prompt", _StubPrompt)

    captured: dict[str, object] = {}

    async def _fake_rsc(change, **kw):
        captured.update(kw)
        return 0

    monkeypatch.setattr(main, "run_session_client", _fake_rsc)

    rc = await main._run_cleared_session(
        tmp_path, "main", leave_state={})
    assert rc == 0
    assert captured["model"] == "anthropic:claude-fable-5"


class _CaptureToolbarPrompt:
    """Captures the bottom_toolbar passed to ask_async; returns a fixed non-slash line."""

    def __init__(self, line: str) -> None:
        self._line = line
        self.toolbar: object = "unset"

    async def ask_async(
        self, prompt_text: str | None = None, *,
        bottom_toolbar: object = None, **kwargs: object,
    ) -> str:
        self.toolbar = bottom_toolbar
        return self._line


@pytest.mark.anyio
async def test_opening_toolbar_composed_from_change_when_no_driver() -> None:
    from imp.cli.slash import ask_line_with_slash

    change = SimpleNamespace(id="lufu50", title="Instant opening identity bar")
    prompt = _CaptureToolbarPrompt("add oauth")
    line = await ask_line_with_slash(prompt, None, change, color=False, width=80)
    assert line == "add oauth"
    assert prompt.toolbar is not None          # was None (no driver) before the change
    assert "lufu50" in prompt.toolbar
    assert "Instant opening identity bar" in prompt.toolbar


@pytest.mark.anyio
async def test_opening_toolbar_id_only_when_title_empty() -> None:
    from imp.cli.slash import ask_line_with_slash

    change = SimpleNamespace(id="lufu50", title="")
    prompt = _CaptureToolbarPrompt("add oauth")
    line = await ask_line_with_slash(prompt, None, change, color=False, width=80)
    assert line == "add oauth"
    assert prompt.toolbar is not None          # empty title still paints the id bar
    assert "lufu50" in prompt.toolbar
