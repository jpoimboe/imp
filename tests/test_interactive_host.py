from __future__ import annotations

from pathlib import Path

import pytest

from imp.agents.interactive import build_setup_input_fn, handle_setup_slash
from imp.agents.transcript import SkillReply
from imp.cli.slash import REPROMPT, SANDBOX_STATE_OFF, SANDBOX_STATE_ON


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _skills_dir(tmp_path: Path) -> Path:
    user = tmp_path / "skills" / "user"
    user.mkdir(parents=True)
    (user / "greet.md").write_text(
        "---\ndescription: say hi\n---\nGreet the user warmly.\n")
    return tmp_path


def test_setup_slash_help_reprompts(tmp_path):
    out: list[str] = []
    result = handle_setup_slash(
        "/help", workflow_dir=tmp_path, output_fn=out.append, unsandboxed=False)
    assert result is REPROMPT
    assert any("Setup commands:" in line for line in out)


def test_setup_slash_sandbox_status_reflects_flag(tmp_path):
    on: list[str] = []
    handle_setup_slash("/sandbox", workflow_dir=tmp_path,
                       output_fn=on.append, unsandboxed=False)
    assert on == [SANDBOX_STATE_ON]
    off: list[str] = []
    handle_setup_slash("/sandbox", workflow_dir=tmp_path,
                       output_fn=off.append, unsandboxed=True)
    assert off == [SANDBOX_STATE_OFF]


@pytest.mark.parametrize(
    "cmd", ["save", "abandon", "shelve", "clear", "compact", "model"])
def test_setup_slash_lifecycle_not_available(tmp_path, cmd):
    out: list[str] = []
    result = handle_setup_slash(
        f"/{cmd}", workflow_dir=tmp_path, output_fn=out.append, unsandboxed=False)
    assert result is REPROMPT
    assert out == [f"/{cmd} is not available during setup."]


def test_setup_slash_lists_user_skills(tmp_path):
    wd = _skills_dir(tmp_path)
    out: list[str] = []
    result = handle_setup_slash(
        "/skills", workflow_dir=wd, output_fn=out.append, unsandboxed=False)
    assert result is REPROMPT
    assert any("greet" in line for line in out)


def test_setup_slash_loads_user_skill_body(tmp_path):
    wd = _skills_dir(tmp_path)
    out: list[str] = []
    result = handle_setup_slash(
        "/greet", workflow_dir=wd, output_fn=out.append, unsandboxed=False)
    assert isinstance(result, SkillReply)
    assert result.name == "greet"
    assert "Greet the user warmly." in result.body   # frontmatter stripped


def test_setup_slash_loads_user_skill_body_with_args(tmp_path):
    wd = _skills_dir(tmp_path)
    out: list[str] = []
    result = handle_setup_slash(
        "/greet in Spanish", workflow_dir=wd, output_fn=out.append, unsandboxed=False)
    assert isinstance(result, SkillReply)
    assert result.name == "greet"
    assert "Greet the user warmly." in result.body
    assert result.body.endswith("\n\nin Spanish\n")


def test_setup_slash_unknown_reprompts(tmp_path):
    out: list[str] = []
    result = handle_setup_slash(
        "/bogus", workflow_dir=tmp_path, output_fn=out.append, unsandboxed=False)
    assert result is REPROMPT
    assert any("Unknown command: /bogus" in line for line in out)


def test_setup_slash_passes_path_shaped_verbatim(tmp_path):
    out: list[str] = []
    result = handle_setup_slash(
        "/usr/local/bin/py", workflow_dir=tmp_path,
        output_fn=out.append, unsandboxed=False)
    assert result == "/usr/local/bin/py"   # path-shaped answer submitted verbatim
    assert out == []                        # not treated as an unknown command


@pytest.mark.anyio
async def test_setup_input_returns_plain_text(tmp_path):
    lines = iter(["light please"])

    async def read_line(_prompt: str) -> str:
        return next(lines)

    fn = build_setup_input_fn(
        read_line=read_line, workflow_dir=tmp_path,
        output_fn=lambda _m: None, unsandboxed=False)
    assert await fn("") == "light please"


@pytest.mark.anyio
async def test_setup_input_loops_past_slash_then_returns(tmp_path):
    lines = iter(["/help", "the real answer"])

    async def read_line(_prompt: str) -> str:
        return next(lines)

    out: list[str] = []
    fn = build_setup_input_fn(
        read_line=read_line, workflow_dir=tmp_path,
        output_fn=out.append, unsandboxed=False)
    assert await fn("") == "the real answer"     # slash consumed, next line returned
    assert any("Setup commands:" in line for line in out)


@pytest.mark.anyio
async def test_setup_input_returns_skill_reply(tmp_path):
    wd = _skills_dir(tmp_path)
    lines = iter(["/greet"])

    async def read_line(_prompt: str) -> str:
        return next(lines)

    fn = build_setup_input_fn(
        read_line=read_line, workflow_dir=wd,
        output_fn=lambda _m: None, unsandboxed=False)
    result = await fn("")
    assert isinstance(result, SkillReply)
    assert result.name == "greet"
