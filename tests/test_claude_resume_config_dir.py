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
    session_store_for,
)
from tests.cc_fakes import FakeClaudeSDKClient, assistant, result, tool_use
from tests.test_claude_runner import _inv, _seed_complete_step_resume

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


@pytest.fixture
def overwrites(monkeypatch: pytest.MonkeyPatch) -> list[list[dict[str, Any]]]:
    """Record every projection ImpSessionStore.overwrite writes."""
    written: list[list[dict[str, Any]]] = []
    orig = ImpSessionStore.overwrite

    async def spy(self: ImpSessionStore, key: Any, entries: list[dict[str, Any]]) -> None:
        written.append(entries)
        await orig(self, key, entries)

    monkeypatch.setattr(ImpSessionStore, "overwrite", spy)
    return written


def _run_factory_runner(tmp_path: Path, seen: dict[str, Any], **kw: Any) -> ClaudeAgentRunner:
    return ClaudeAgentRunner(model_name="claude-sonnet-4-5",
                             client_factory=_snapshot_factory(tmp_path, seen), **kw)


@pytest.mark.anyio
async def test_resume_dir_holds_the_projection_not_a_stale_mirror(
    tmp_path: Path, overwrites: list[list[dict[str, Any]]],
) -> None:
    inv = _send_to_llm_inv(tmp_path)
    store = session_store_for(inv.transcript_dir)
    stale = [{"type": "user" if i % 2 == 0 else "assistant", "uuid": f"d{i}",
              "message": {"role": "user" if i % 2 == 0 else "assistant", "content": "x"}}
             for i in range(4)]
    await store.append(session_key(tmp_path, AID), stale)
    seen: dict[str, Any] = {}
    [ev async for ev in _run_factory_runner(tmp_path, seen).run(inv)]

    assert len(overwrites) == 1
    assert seen["lines"] == overwrites[0]
    assert len(seen["lines"]) == 2                      # messages[:resume_base]
    assert not {"d0", "d1", "d2", "d3"} & {r.get("uuid") for r in seen["lines"]}


@pytest.mark.anyio
async def test_resume_dir_holds_the_projection_when_the_store_was_absent(
    tmp_path: Path, overwrites: list[list[dict[str, Any]]],
) -> None:
    inv = _send_to_llm_inv(tmp_path)
    seen: dict[str, Any] = {}
    [ev async for ev in _run_factory_runner(tmp_path, seen).run(inv)]

    assert len(overwrites) == 1
    assert seen["lines"] == overwrites[0]
    assert len(seen["lines"]) == 2


@pytest.mark.anyio
async def test_resume_dir_holds_the_synthesized_tool_result(
    tmp_path: Path, overwrites: list[list[dict[str, Any]]],
) -> None:
    # Dangling complete_step tail: answer_complete_step_on_resume folds a
    # tool_result for c0 into the loaded body.
    inv = _seed_complete_step_resume(tmp_path, retry_feedback=None)
    seen: dict[str, Any] = {}
    [ev async for ev in _run_factory_runner(tmp_path, seen).run(inv)]

    assert len(overwrites) == 1
    assert seen["lines"] == overwrites[0]
    results = [
        b for r in seen["lines"]
        for b in (r.get("message", {}).get("content") or [])
        if isinstance(b, dict) and b.get("type") == "tool_result"]
    assert [b["tool_use_id"] for b in results] == ["c0"]


@pytest.mark.anyio
async def test_awaiting_user_resume_copies_auth_after_the_reply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, auth_src: Path,
) -> None:
    inv = _resume_inv(
        tmp_path,
        Message(role="user", content=(TextBlock(text="go"),)),
        Message(role="assistant", content=(TextBlock(text="done early"),)))

    def _reply(_prompt: str) -> str:
        _write_creds(auth_src, "tok-new")   # a peer refreshes while the reply is pending
        return "keep going"

    seen: dict[str, Any] = {}
    runner = _run_factory_runner(tmp_path, seen, user_input_fn=_reply)
    at_build: dict[str, bool] = {}
    orig_build = runner._build_options

    def _spy_build(*a: Any, **k: Any) -> Any:
        opts = orig_build(*a, **k)
        at_build["creds"] = (Path(opts.env["CLAUDE_CONFIG_DIR"]) / ".credentials.json").exists()
        return opts

    monkeypatch.setattr(runner, "_build_options", _spy_build)
    events = [ev async for ev in runner.run(inv)]

    assert events[-1]["type"] == "agent_result"
    assert at_build == {"creds": False}
    assert seen["creds"]["claudeAiOauth"]["accessToken"] == "tok-new"
    assert "refreshToken" not in seen["creds"]["claudeAiOauth"]


@pytest.mark.anyio
async def test_a_failed_fill_leaves_no_resume_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Scan the per-test temp root below, not an inherited shared IMP_QUEST_TMP.
    monkeypatch.delenv("IMP_QUEST_TMP", raising=False)
    calls: list[tuple[Path, bool]] = []

    async def _boom(config_dir: Path, *_a: Any) -> None:
        calls.append((config_dir, config_dir.is_dir()))
        raise RuntimeError("fill failed")

    monkeypatch.setattr(claude, "fill_resume_config_dir", _boom)
    built: list[Any] = []

    def factory(opts: Any) -> FakeClaudeSDKClient:
        built.append(opts)
        return FakeClaudeSDKClient(opts, scripted_messages=[result()])

    runner = ClaudeAgentRunner(model_name="claude-sonnet-4-5", client_factory=factory)
    events = [ev async for ev in runner.run(_send_to_llm_inv(tmp_path))]

    assert any(e["type"] == "error" for e in events)
    assert not any(e["type"] == "agent_result" for e in events)
    assert built == []                                   # never spawned
    assert len(calls) == 1 and calls[0][1] is True       # the dir existed when fill ran
    assert not calls[0][0].exists()
    assert runner._config_dir is None
    assert not [p for p in quest_scratch_root().iterdir()
                if p.name.startswith("claude-resume-")]
