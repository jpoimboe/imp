from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest

from imp.agents.interactive import (
    SANDBOX_SETUP_OFF,
    SANDBOX_SETUP_ON,
    build_setup_input_fn,
    handle_setup_slash,
)
from imp.agents.transcript import SkillReply
from imp.cli.slash import REPROMPT


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _skills_dir(tmp_path: Path) -> Path:
    user = tmp_path / "skills" / "user"
    user.mkdir(parents=True)
    (user / "greet.md").write_text(
        "---\ndescription: say hi\n---\nGreet the user warmly.\n")
    return tmp_path


class _FakePrompt:
    """Stand-in for cli.prompt.Prompt: hands out queued lines and records how
    each read was framed."""

    def __init__(self, lines: list[str] | None = None) -> None:
        self._lines = list(lines or [])
        self.prompt_texts: list[str | None] = []
        self.bottom_toolbars: list[Any] = []
        self.hints: list[str | None] = []

    async def ask_async(self, prompt_text: str | None = None, *,
                        question: str = "", width: int = 80, indent: str = "",
                        bottom_toolbar: Any = None,
                        hint: str | None = None) -> str:
        self.prompt_texts.append(prompt_text)
        self.bottom_toolbars.append(bottom_toolbar)
        self.hints.append(hint)
        assert self._lines, "unexpected prompt read"
        return self._lines.pop(0)


class _StubDriver:
    def __init__(self, toolbar: str | None = None) -> None:
        self.events: list[str] = []
        self._toolbar = toolbar

    def prepare_for_prompt(self) -> None:
        self.events.append("prepare")

    def flush_deferred(self) -> None:
        self.events.append("flush")

    def prompt_indent(self) -> str:
        return ""

    def compose_toolbar_static(self) -> str | None:
        return self._toolbar


class _CountStatus:
    def __init__(self) -> None:
        self.suspend_calls = 0

    def suspend(self) -> object:
        import contextlib
        self.suspend_calls += 1
        return contextlib.nullcontext()


def test_setup_slash_help_reprompts(tmp_path):
    out: list[str] = []
    result = handle_setup_slash(
        "/help", asset_dir=tmp_path, output_fn=out.append, unsandboxed=False)
    assert result is REPROMPT
    assert any("Setup commands:" in line for line in out)


def test_setup_slash_sandbox_status_reflects_flag(tmp_path):
    on: list[str] = []
    handle_setup_slash("/sandbox", asset_dir=tmp_path,
                       output_fn=on.append, unsandboxed=False)
    assert on == [SANDBOX_SETUP_ON]
    off: list[str] = []
    handle_setup_slash("/sandbox", asset_dir=tmp_path,
                       output_fn=off.append, unsandboxed=True)
    assert off == [SANDBOX_SETUP_OFF]


@pytest.mark.parametrize(
    "cmd", ["save", "abandon", "shelve", "clear", "compact", "model"])
def test_setup_slash_lifecycle_not_available(tmp_path, cmd):
    out: list[str] = []
    result = handle_setup_slash(
        f"/{cmd}", asset_dir=tmp_path, output_fn=out.append, unsandboxed=False)
    assert result is REPROMPT
    assert out == [f"/{cmd} is not available during setup."]


def test_setup_slash_lists_user_skills(tmp_path):
    wd = _skills_dir(tmp_path)
    out: list[str] = []
    result = handle_setup_slash(
        "/skills", asset_dir=wd, output_fn=out.append, unsandboxed=False)
    assert result is REPROMPT
    assert any("greet" in line for line in out)


def test_setup_slash_loads_user_skill_body(tmp_path):
    wd = _skills_dir(tmp_path)
    out: list[str] = []
    result = handle_setup_slash(
        "/greet", asset_dir=wd, output_fn=out.append, unsandboxed=False)
    assert isinstance(result, SkillReply)
    assert result.name == "greet"
    assert "Greet the user warmly." in result.body   # frontmatter stripped


def test_setup_slash_loads_user_skill_body_with_args(tmp_path):
    wd = _skills_dir(tmp_path)
    out: list[str] = []
    result = handle_setup_slash(
        "/greet in Spanish", asset_dir=wd, output_fn=out.append, unsandboxed=False)
    assert isinstance(result, SkillReply)
    assert result.name == "greet"
    assert "Greet the user warmly." in result.body
    assert result.body.endswith("\n\nin Spanish\n")


def test_setup_slash_unknown_reprompts(tmp_path):
    out: list[str] = []
    result = handle_setup_slash(
        "/bogus", asset_dir=tmp_path, output_fn=out.append, unsandboxed=False)
    assert result is REPROMPT
    assert any("Unknown command: /bogus" in line for line in out)


def test_setup_slash_passes_path_shaped_verbatim(tmp_path):
    out: list[str] = []
    result = handle_setup_slash(
        "/usr/local/bin/py", asset_dir=tmp_path,
        output_fn=out.append, unsandboxed=False)
    assert result == "/usr/local/bin/py"   # path-shaped answer submitted verbatim
    assert out == []                        # not treated as an unknown command


@pytest.mark.anyio
async def test_setup_input_returns_plain_text(tmp_path):
    fn = build_setup_input_fn(
        prompt=_FakePrompt(["light please"]), asset_dir=tmp_path,
        output_fn=lambda _m: None, unsandboxed=False,
        status=None, driver=_StubDriver(), coord=None)
    assert await fn("") == "light please"


@pytest.mark.anyio
async def test_setup_input_loops_past_slash_then_returns(tmp_path):
    out: list[str] = []
    fn = build_setup_input_fn(
        prompt=_FakePrompt(["/help", "the real answer"]), asset_dir=tmp_path,
        output_fn=out.append, unsandboxed=False,
        status=None, driver=_StubDriver(), coord=None)
    assert await fn("") == "the real answer"     # slash consumed, next line returned
    assert any("Setup commands:" in line for line in out)


@pytest.mark.anyio
async def test_setup_input_returns_skill_reply(tmp_path):
    wd = _skills_dir(tmp_path)
    fn = build_setup_input_fn(
        prompt=_FakePrompt(["/greet"]), asset_dir=wd,
        output_fn=lambda _m: None, unsandboxed=False,
        status=None, driver=_StubDriver(), coord=None)
    result = await fn("")
    assert isinstance(result, SkillReply)
    assert result.name == "greet"


# ---------------------------------------------------------------------------
# Host tests (Task 2)
# ---------------------------------------------------------------------------

from imp.agents.interactive import (  # noqa: E402
    InteractiveFailure,
    InteractiveSuccess,
    run_interactive_agent,
    run_interactive_agent_sync,
)
from imp.engine.sandbox import SandboxConfig  # noqa: E402
from imp.engine.state import InMemoryEventLog  # noqa: E402
from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc  # noqa: E402


class _NonTTY(io.StringIO):
    def isatty(self) -> bool:
        return False


def _agent_def() -> AgentDef:
    return AgentDef(
        name="init", description="", tools=frozenset(), subagents=(),
        output_schema=OutputSchema(
            fields=(
                OutputField(name="status", type="enum",
                            values=("Success", "Error"), required=True),
                OutputField(name="default_workflow", type="string", required=True),
            ),
            source=SourceLoc(file=Path("a.md"), line=1),
        ),
        prompt_template_source="help configure imp for this project",
        source_path=Path("a.md"),
        source=SourceLoc(file=Path("a.md"), line=1),
    )


class _ScriptedHostRunner:
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self._events = events

    async def run(self, invocation: Any):
        for ev in self._events:
            yield ev

    async def interrupt(self) -> None:
        pass


class _RenderingRunner:
    """Appends a minimal renderable narrative sequence to invocation.event_log
    (proving the InMemoryEventLog->LiveDriver fan-out) before terminating."""

    async def run(self, invocation: Any):
        log = invocation.event_log
        log.append(agent_id=None, type="quest_created",
                   payload={"quest_id": 1, "title": "setup"})
        log.append(agent_id=None, type="step_started",
                   payload={"step": "s", "kind": "agent", "phase": "setup",
                            "model": "m", "provider": "anthropic"})
        log.append(agent_id="a", type="agent_message",
                   payload={"raw": {"content": [
                       {"type": "text", "text": "RENDER-MARKER"}]}, "usage": {}})
        log.append(agent_id="a", type="step_completed",
                   payload={"step": "s", "kind": "agent",
                            "action": {"verb": "continue"}, "var_mutations": {}})
        yield {"type": "agent_result",
               "payload": {"status": "Success", "default_workflow": "light"}}

    async def interrupt(self) -> None:
        pass


def _register_runner(provider_registry, runner) -> None:
    class _Provider:
        name = "fake"

        def __init__(self, model: str = "m") -> None:
            self.model = model

        def create_runner(self, invocation: Any):
            return runner

    provider_registry("fake", env_check=lambda: True,
                      factory=lambda model: _Provider(model))


@pytest.mark.anyio
async def test_host_returns_agent_output(tmp_path, provider_registry):
    _register_runner(provider_registry, _ScriptedHostRunner([
        {"type": "agent_result",
         "payload": {"status": "Success", "default_workflow": "light"}},
    ]))
    result = await run_interactive_agent(
        agent_def=_agent_def(), provider_name="fake", model_name="m",
        initial_message="go", project_root=tmp_path, asset_dir=tmp_path,
        sandbox_cfg=SandboxConfig(allow_unsandboxed=True),
        prompt=_FakePrompt(), stream=_NonTTY())
    assert isinstance(result, InteractiveSuccess)
    assert result.output["default_workflow"] == "light"


@pytest.mark.anyio
async def test_host_builds_attended_invocation(tmp_path, provider_registry):
    captured: list[Any] = []

    class _Capturing:
        name = "fake"
        model = "m"

        def create_runner(self, invocation: Any):
            captured.append(invocation)
            return _ScriptedHostRunner([
                {"type": "agent_result",
                 "payload": {"status": "Success", "default_workflow": "default"}},
            ])

    provider_registry("fake", env_check=lambda: True,
                      factory=lambda model: _Capturing())
    await run_interactive_agent(
        agent_def=_agent_def(), provider_name="fake", model_name="m",
        initial_message="go", project_root=tmp_path, asset_dir=tmp_path,
        sandbox_cfg=SandboxConfig(allow_unsandboxed=True),
        prompt=_FakePrompt(), stream=_NonTTY())
    assert captured, "create_runner never called"
    inv = captured[0]
    assert inv.interactive is True
    assert inv.headless is False
    assert inv.user_input_fn is not None
    assert isinstance(inv.event_log, InMemoryEventLog)
    assert inv.coordinator is not None


@pytest.mark.anyio
async def test_host_fans_events_to_terminal(tmp_path, provider_registry):
    out = _NonTTY()
    _register_runner(provider_registry, _RenderingRunner())
    await run_interactive_agent(
        agent_def=_agent_def(), provider_name="fake", model_name="m",
        initial_message="go", project_root=tmp_path, asset_dir=tmp_path,
        sandbox_cfg=SandboxConfig(allow_unsandboxed=True),
        prompt=_FakePrompt(), stream=out)
    assert "RENDER-MARKER" in out.getvalue()


@pytest.mark.anyio
async def test_host_auth_error_is_failure(tmp_path, provider_registry):
    _register_runner(provider_registry, _ScriptedHostRunner([
        {"type": "error",
         "payload": {"category": "auth", "kind": "bad_key", "message": "no"}},
    ]))
    result = await run_interactive_agent(
        agent_def=_agent_def(), provider_name="fake", model_name="m",
        initial_message="go", project_root=tmp_path, asset_dir=tmp_path,
        sandbox_cfg=SandboxConfig(allow_unsandboxed=True),
        prompt=_FakePrompt(), stream=_NonTTY())
    assert isinstance(result, InteractiveFailure)
    assert result.category == "auth"
    assert result.detail == "bad_key"


def test_host_sync_wrapper_runs(tmp_path, provider_registry):
    _register_runner(provider_registry, _ScriptedHostRunner([
        {"type": "agent_result",
         "payload": {"status": "Success", "default_workflow": "default"}},
    ]))
    result = run_interactive_agent_sync(
        agent_def=_agent_def(), provider_name="fake", model_name="m",
        initial_message="go", project_root=tmp_path, asset_dir=tmp_path,
        sandbox_cfg=SandboxConfig(allow_unsandboxed=True),
        prompt=_FakePrompt(), stream=_NonTTY())
    assert isinstance(result, InteractiveSuccess)
    assert result.output["default_workflow"] == "default"


def test_setup_host_api_takes_asset_dir_not_workflow_dir() -> None:
    import inspect

    from imp.agents import interactive
    for fn in (interactive.handle_setup_slash, interactive.build_setup_input_fn,
               interactive.run_interactive_agent):
        params = inspect.signature(fn).parameters
        assert "asset_dir" in params, fn.__name__
        assert "workflow_dir" not in params, fn.__name__


def test_setup_slash_skills_empty_under_the_assets_root() -> None:
    from imp.assets import bundled_assets_dir
    out: list[str] = []
    result = handle_setup_slash(
        "/skills", asset_dir=bundled_assets_dir(),
        output_fn=out.append, unsandboxed=False)
    assert result is REPROMPT
    assert out == ["No skills available."]


@pytest.mark.anyio
async def test_setup_read_uses_the_default_caret_and_placeholder(tmp_path):
    prompt = _FakePrompt(["dark"])
    fn = build_setup_input_fn(
        prompt=prompt, asset_dir=tmp_path, output_fn=lambda _m: None,
        unsandboxed=False, status=None, driver=_StubDriver(), coord=None)
    assert await fn("") == "dark"
    # None — not the "" the setup loop used to pass — is what makes
    # Prompt._message render the gold `> ` caret; hint=None leaves the
    # `type a message, or /help` placeholder in place.
    assert prompt.prompt_texts == [None]
    assert prompt.hints == [None]


@pytest.mark.anyio
async def test_setup_field_carries_the_drivers_static_toolbar(tmp_path):
    from imp.cli.live_driver import LiveDriver
    from imp.cli.live_status import LiveStatus

    out = _NonTTY()
    driver = LiveDriver(stream=out, color=False, width=80,
                        status=LiveStatus(out), title="imp setup")
    prompt = _FakePrompt(["dark"])
    fn = build_setup_input_fn(
        prompt=prompt, asset_dir=tmp_path, output_fn=lambda _m: None,
        unsandboxed=False, status=None, driver=driver, coord=None)
    assert await fn("") == "dark"
    assert prompt.bottom_toolbars == ["imp setup"]   # title-only identity band


@pytest.mark.anyio
async def test_setup_slash_output_is_followed_by_one_blank_and_a_reprompt(tmp_path):
    prompt = _FakePrompt(["/help", "the real answer"])
    driver = _StubDriver()
    status = _CountStatus()
    out: list[str] = []
    fn = build_setup_input_fn(
        prompt=prompt, asset_dir=tmp_path, output_fn=out.append,
        unsandboxed=False, status=status, driver=driver, coord=None)
    assert await fn("") == "the real answer"
    assert any("Setup commands:" in line for line in out)
    assert out[-1] == ""                          # one blank before the reprompt
    assert driver.events == ["prepare", "prepare", "flush"]
    assert status.suspend_calls == 1              # one hoisted suspend per turn


@pytest.mark.anyio
async def test_setup_reprompts_on_a_bare_enter(tmp_path):
    prompt = _FakePrompt(["", "the real answer"])
    fn = build_setup_input_fn(
        prompt=prompt, asset_dir=tmp_path, output_fn=lambda _m: None,
        unsandboxed=False, status=None, driver=_StubDriver(), coord=None)
    # An empty answer used to be handed to the agent verbatim; the shared loop
    # re-reads instead, so the agent only ever sees a real line.
    assert await fn("") == "the real answer"
    assert len(prompt.hints) == 2                 # one entry per read


@pytest.mark.anyio
async def test_setup_input_routes_through_the_shared_framing_helper(
    tmp_path, monkeypatch,
):
    from imp.agents import interactive as interactive_mod

    seen: list[dict[str, Any]] = []

    async def _fake_solicit(prompt: Any, status: Any, **kwargs: Any) -> object:
        seen.append(kwargs)
        return "framed"

    monkeypatch.setattr(interactive_mod, "solicit_line", _fake_solicit)
    driver = _StubDriver()
    fn = build_setup_input_fn(
        prompt=_FakePrompt([]), asset_dir=tmp_path, output_fn=lambda _m: None,
        unsandboxed=False, status=None, driver=driver, coord=None)
    assert await fn("") == "framed"
    assert seen[0]["driver"] is driver
    assert callable(seen[0]["dispatch"])
