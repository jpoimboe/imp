"""A native resume runs under an imp-made claude-resume-* dir, filled right before spawn."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from imp.agents.runner import AgentInvocation, Message, TextBlock
from imp.agents.transcript import Transcript
from imp.engine.env import quest_scratch_root
from imp.providers import claude
from imp.providers import claude_token_refresh as ctr
from imp.providers.claude import ClaudeAgentRunner
from imp.providers.claude_session_store import (
    ImpSessionStore,
    resume_session_id,
    session_key,
)
from tests.cc_fakes import FakeClaudeSDKClient, assistant, result, tool_use
from tests.test_claude_runner import _inv

AID = "step:p/s"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _write_creds(base: Path, access_token: str) -> None:
    base.mkdir(parents=True, exist_ok=True)
    (base / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {
        "accessToken": access_token, "refreshToken": "never-copied",
        "expiresAt": 4_102_444_800_000}}))


@pytest.fixture(autouse=True)
def auth_src(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    src = tmp_path / "auth-src"
    _write_creds(src, "tok-old")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(src))
    monkeypatch.setattr(claude, "refresh_canonical_token",
                        lambda *_a, **_k: ctr.RefreshOutcome(ctr.SKIPPED_FRESH))
    return src


def _resume_inv(tmp_path: Path, *msgs: Message) -> AgentInvocation:
    (tmp_path / "art").mkdir()
    inv = _inv(tmp_path, agent_id=AID)
    inv.transcript_dir.mkdir(parents=True, exist_ok=True)
    t = Transcript(inv.transcript_dir / "transcript.jsonl")
    for m in msgs:
        t.append(m)
    return inv


def _send_to_llm_inv(tmp_path: Path) -> AgentInvocation:
    """SEND_TO_LLM tail: native resume with resume_base == 2."""
    return _resume_inv(
        tmp_path,
        Message(role="user", content=(TextBlock(text="go"),)),
        Message(role="assistant", content=(TextBlock(text="hi"),)),
        Message(role="user", content=(TextBlock(text="keep going"),)))


def _snapshot_factory(tmp_path: Path, seen: dict[str, Any]) -> Any:
    """Record the resume dir's contents at the moment the client is built."""
    def factory(opts: Any) -> FakeClaudeSDKClient:
        cfg = Path(opts.env["CLAUDE_CONFIG_DIR"])
        pkey = session_key(tmp_path, AID)["project_key"]
        jsonl = cfg / "projects" / pkey / f"{resume_session_id(AID)}.jsonl"
        seen["opts"] = opts
        seen["cfg"] = cfg
        seen["lines"] = [json.loads(x) for x in jsonl.read_text().splitlines()]
        seen["creds"] = json.loads((cfg / ".credentials.json").read_text())
        return FakeClaudeSDKClient(opts, scripted_messages=[
            assistant(tool_use("c1", "complete_step", {"status": "Success"})), result()])
    return factory


@pytest.mark.anyio
async def test_native_resume_options_shape_and_dir_lifecycle(tmp_path: Path) -> None:
    from claude_agent_sdk._internal.session_resume import materialize_resume_session

    inv = _send_to_llm_inv(tmp_path)
    seen: dict[str, Any] = {}
    runner = ClaudeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=_snapshot_factory(tmp_path, seen))
    events = [ev async for ev in runner.run(inv)]

    assert events[-1]["type"] == "agent_result"
    opts = seen["opts"]
    assert opts.resume is None
    assert opts.session_id is None
    assert opts.continue_conversation is False
    assert opts.extra_args["resume"] == resume_session_id(AID)
    assert seen["cfg"].parent == quest_scratch_root()
    assert seen["cfg"].name.startswith("claude-resume-")
    assert isinstance(opts.session_store, ImpSessionStore)
    # The SDK sees no options.resume, so it never mkdtemps its own config dir.
    assert await materialize_resume_session(opts) is None
    assert not seen["cfg"].exists()           # run()'s finally removed it
    assert runner._config_dir is None
