"""AC 8: the pre-spawn token refresh fires ahead of both spawn dispositions and
journals one audit event per check."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from imp.agents.runner import AgentInvocation, Message, QuestContext, TextBlock
from imp.agents.transcript import Transcript
from imp.engine.state import InMemoryEventLog
from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc
from imp.paths import project_state_dir
from imp.providers import claude_code
from imp.providers import claude_token_refresh as ctr
from imp.providers.claude_code import ClaudeCodeAgentRunner
from tests.cc_fakes import FakeClaudeSDKClient, assistant, result, text, tool_use

NOW = 1_000_000_000_000


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _agent_def() -> AgentDef:
    return AgentDef(
        name="t", description="", tools=frozenset({"complete_step"}),
        subagents=(),
        output_schema=OutputSchema(
            fields=(OutputField(name="status", type="enum",
                                values=("Success", "Error"), required=True),),
            source=SourceLoc(file=Path("a.md"), line=1),
        ),
        prompt_template_source="hi",
        source_path=Path("a.md"),
        source=SourceLoc(file=Path("a.md"), line=1),
    )


def _inv(tmp_path: Path, log: InMemoryEventLog) -> AgentInvocation:
    return AgentInvocation(
        agent_def=_agent_def(),
        provider_name="claude_code", model_name="claude-sonnet-4-5",
        system_prompt="be terse",
        initial_messages=(Message(role="user", content=(TextBlock(text="go"),)),),
        quest_context=QuestContext(
            title="Test", project_root=tmp_path,
            workflow_dir=project_state_dir(tmp_path) / "quests" / "test-quest" / "workflow"),
        transcript_dir=tmp_path / "step",
        events_path=tmp_path / "events.jsonl",
        agent_id="step:p/s",
        event_log=log,
    )


def _complete_step_client(opts: Any) -> FakeClaudeSDKClient:
    return FakeClaudeSDKClient(opts, scripted_messages=[
        assistant(tool_use("c1", "complete_step", {"status": "Success"})),
        result(),
    ])


@pytest.mark.anyio
async def test_fresh_spawn_refreshes_before_config_dir_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "art").mkdir()
    order: list[str] = []

    def _fake_refresh(now_ms: int, *, env: Any = None,
                      force: bool = False) -> ctr.RefreshOutcome:
        order.append("refresh")
        return ctr.RefreshOutcome(ctr.REFRESHED, expires_at=NOW + 3600 * 1000)

    def _fake_materialize() -> Path:
        order.append("materialize")
        cfg = tmp_path / "cfg"
        (cfg / "projects").mkdir(parents=True, exist_ok=True)
        return cfg

    monkeypatch.setattr(claude_code, "refresh_canonical_token", _fake_refresh)
    monkeypatch.setattr(
        claude_code, "materialize_fresh_config_dir", _fake_materialize)

    log = InMemoryEventLog()
    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=_complete_step_client)
    [ev async for ev in runner.run(_inv(tmp_path, log))]

    # Refresh runs BEFORE the fresh disposition copies canonical.
    assert order == ["refresh", "materialize"]
    events = [e for e in log.read() if e["type"] == "token_refresh"]
    assert len(events) == 1
    assert events[0]["agent_id"] == "step:p/s"
    # Exact-dict equality proves the payload carries ONLY outcome + expires_at —
    # no token material can appear.
    assert events[0]["payload"] == {
        "outcome": "refreshed", "expires_at": NOW + 3600 * 1000}


@pytest.mark.anyio
async def test_native_resume_refreshes_without_materializing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "art").mkdir()
    calls: list[str] = []

    def _fake_refresh(now_ms: int, *, env: Any = None,
                      force: bool = False) -> ctr.RefreshOutcome:
        calls.append("refresh")
        return ctr.RefreshOutcome(ctr.SKIPPED_FRESH)

    def _fake_materialize() -> Path:
        calls.append("materialize")
        raise AssertionError("native resume must not copy a fresh config dir")

    monkeypatch.setattr(claude_code, "refresh_canonical_token", _fake_refresh)
    monkeypatch.setattr(
        claude_code, "materialize_fresh_config_dir", _fake_materialize)

    log = InMemoryEventLog()
    inv = _inv(tmp_path, log)
    inv.transcript_dir.mkdir(parents=True, exist_ok=True)
    # Seed a resume-eligible transcript: assistant text tail + trailing plain-text
    # user turn drives the native-resume disposition (config_dir stays None).
    t = Transcript(inv.transcript_dir / "transcript.jsonl")
    t.append(Message(role="user", content=(TextBlock(text="go"),)))
    t.append(Message(role="assistant", content=(TextBlock(text="hi"),)))
    t.append(Message(role="user", content=(TextBlock(text="keep going"),)))

    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=_complete_step_client)
    [ev async for ev in runner.run(inv)]

    # Refresh fired; materialize was never reached (native path).
    assert calls == ["refresh"]
    events = [e for e in log.read() if e["type"] == "token_refresh"]
    assert len(events) == 1
    assert events[0]["payload"] == {"outcome": "skipped_fresh"}


@pytest.mark.anyio
async def test_dead_token_raises_terminal_auth_without_materializing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "art").mkdir()

    def _dead(now_ms: int, *, env: Any = None,
              force: bool = False) -> ctr.RefreshOutcome:
        return ctr.RefreshOutcome(ctr.FAILED_INVALID_GRANT)

    def _fake_materialize() -> Path:
        raise AssertionError("dead token must short-circuit before config copy")

    monkeypatch.setattr(claude_code, "refresh_canonical_token", _dead)
    monkeypatch.setattr(
        claude_code, "materialize_fresh_config_dir", _fake_materialize)

    log = InMemoryEventLog()
    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=_complete_step_client)
    events = [ev async for ev in runner.run(_inv(tmp_path, log))]

    err = events[-1]
    assert err["type"] == "error"
    assert err["payload"]["category"] == "auth_terminal"
    assert err["payload"]["kind"] == "AuthTerminalError"
    # Config dir was never materialized — no claude -p spawn was built.
    refreshes = [e for e in log.read() if e["type"] == "token_refresh"]
    assert refreshes[0]["payload"] == {"outcome": "failed_invalid_grant"}


PROSE_401 = (
    "Failed to authenticate. API Error: 401 OAuth access token has been revoked.")


def _probe_seam(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, forced_outcome: ctr.RefreshOutcome,
) -> list[bool]:
    """Seam: forced calls get forced_outcome, unforced get NOT_APPLICABLE; returns seen flags."""
    seen: list[bool] = []

    def _fake_refresh(now_ms: int, *, env: Any = None,
                      force: bool = False) -> ctr.RefreshOutcome:
        seen.append(force)
        return forced_outcome if force else ctr.RefreshOutcome(ctr.NOT_APPLICABLE)

    def _fake_materialize() -> Path:
        cfg = tmp_path / "cfg"
        (cfg / "projects").mkdir(parents=True, exist_ok=True)
        return cfg

    monkeypatch.setattr(claude_code, "refresh_canonical_token", _fake_refresh)
    monkeypatch.setattr(
        claude_code, "materialize_fresh_config_dir", _fake_materialize)
    return seen


def _run_scripted(tmp_path: Path, log: InMemoryEventLog,
                  scripted: list[Any]) -> Any:
    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5",
        client_factory=lambda o: FakeClaudeSDKClient(o, scripted_messages=scripted))
    return runner.run(_inv(tmp_path, log))


@pytest.mark.anyio
async def test_dead_grant_probe_upgrades_the_auth_error_to_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "art").mkdir()
    seen = _probe_seam(
        monkeypatch, tmp_path, ctr.RefreshOutcome(ctr.FAILED_INVALID_GRANT))

    log = InMemoryEventLog()
    events = [ev async for ev in _run_scripted(
        tmp_path, log, [assistant(text(PROSE_401)), result()])]

    # Pre-spawn refresh unforced; the terminality probe forced.
    assert seen == [False, True]
    errs = [e for e in events if e["type"] == "error"]
    assert [e["payload"]["category"] for e in errs] == ["auth_terminal"]
    assert errs[0]["payload"]["kind"] == "AuthTerminalError"
    assert errs[0]["payload"]["message"] == (
        "claude: OAuth refresh token is dead; re-authentication required")
    # The upgrade inherits the classified error's detail verbatim.
    assert errs[0]["payload"]["detail"]["last_agent_text"] == PROSE_401
    refreshes = [e for e in log.read() if e["type"] == "token_refresh"]
    assert [e["payload"] for e in refreshes] == [
        {"outcome": "not_applicable"}, {"outcome": "failed_invalid_grant"}]


@pytest.mark.anyio
async def test_successful_probe_leaves_the_auth_error_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "art").mkdir()
    seen = _probe_seam(
        monkeypatch, tmp_path,
        ctr.RefreshOutcome(ctr.REFRESHED, expires_at=NOW + 3600 * 1000))

    log = InMemoryEventLog()
    events = [ev async for ev in _run_scripted(
        tmp_path, log, [assistant(text(PROSE_401)), result()])]

    assert seen == [False, True]
    errs = [e for e in events if e["type"] == "error"]
    assert [e["payload"]["category"] for e in errs] == ["auth"]
    assert errs[0]["payload"]["kind"] == "http_401"
    refreshes = [e for e in log.read() if e["type"] == "token_refresh"]
    assert [e["payload"] for e in refreshes] == [
        {"outcome": "not_applicable"},
        {"outcome": "refreshed", "expires_at": NOW + 3600 * 1000}]


@pytest.mark.anyio
async def test_probe_fires_on_the_termination_branch_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "art").mkdir()
    seen = _probe_seam(
        monkeypatch, tmp_path, ctr.RefreshOutcome(ctr.FAILED_INVALID_GRANT))

    log = InMemoryEventLog()
    # api_error_status labels the termination 'auth', exercising _termination_from_result.
    events = [ev async for ev in _run_scripted(tmp_path, log, [
        assistant(text("Session ended.")),
        result(is_error=True, api_error_status=401)])]

    assert seen == [False, True]
    errs = [e for e in events if e["type"] == "error"]
    assert [e["payload"]["category"] for e in errs] == ["auth_terminal"]
    assert errs[0]["payload"]["kind"] == "AuthTerminalError"
    # The merged termination diagnostics survive the type replacement.
    assert errs[0]["payload"]["detail"]["api_error_status"] == 401
    assert errs[0]["payload"]["detail"]["last_agent_text"] == "Session ended."
    refreshes = [e for e in log.read() if e["type"] == "token_refresh"]
    assert [e["payload"] for e in refreshes] == [
        {"outcome": "not_applicable"}, {"outcome": "failed_invalid_grant"}]


@pytest.mark.anyio
async def test_a_non_auth_error_triggers_no_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "art").mkdir()
    seen = _probe_seam(
        monkeypatch, tmp_path, ctr.RefreshOutcome(ctr.FAILED_INVALID_GRANT))

    log = InMemoryEventLog()
    events = [ev async for ev in _run_scripted(
        tmp_path, log, [assistant(text("API Error: 500 upstream is down")),
                        result()])]

    # Only the unforced pre-spawn refresh ran: no probe, no extra journal row.
    assert seen == [False]
    errs = [e for e in events if e["type"] == "error"]
    assert [e["payload"]["category"] for e in errs] == ["network"]
    refreshes = [e for e in log.read() if e["type"] == "token_refresh"]
    assert [e["payload"] for e in refreshes] == [{"outcome": "not_applicable"}]


def _write_canonical(base: Path, expires_at: int) -> None:
    base.mkdir(parents=True, exist_ok=True)
    (base / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "x", "refreshToken": "y",
                                      "expiresAt": expires_at}}),
        encoding="utf-8")


def _canonical_env(monkeypatch: pytest.MonkeyPatch, base: Path) -> None:
    """Point the canonical reader at `base`; the autouse conftest fixture sets
    ANTHROPIC_API_KEY, which would otherwise short-circuit the read to None."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(base))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)


@pytest.mark.anyio
async def test_fresh_spawn_records_the_post_refresh_canonical_expiry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The recorded value is the expiry as of AFTER the pre-spawn refresh: the
    fake refresh rotates the file, and the pre-refresh value must not be what
    lands on the runner."""
    (tmp_path / "art").mkdir()
    canonical = tmp_path / "canonical"
    _write_canonical(canonical, NOW + 60_000)          # pre-refresh
    _canonical_env(monkeypatch, canonical)
    order: list[str] = []

    def _fake_refresh(now_ms: int, *, env: Any = None,
                      force: bool = False) -> ctr.RefreshOutcome:
        order.append("refresh")
        _write_canonical(canonical, NOW + 3600 * 1000)  # the refresh rotates it
        return ctr.RefreshOutcome(ctr.REFRESHED, expires_at=NOW + 3600 * 1000)

    def _fake_materialize() -> Path:
        order.append("materialize")
        cfg = tmp_path / "cfg"
        (cfg / "projects").mkdir(parents=True, exist_ok=True)
        return cfg

    monkeypatch.setattr(claude_code, "refresh_canonical_token", _fake_refresh)
    monkeypatch.setattr(
        claude_code, "materialize_fresh_config_dir", _fake_materialize)

    log = InMemoryEventLog()
    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=_complete_step_client)
    [ev async for ev in runner.run(_inv(tmp_path, log))]

    assert order == ["refresh", "materialize"]   # fresh disposition, refresh first
    assert runner._spawn_token_expiry == NOW + 3600 * 1000


@pytest.mark.anyio
async def test_native_resume_records_the_canonical_expiry_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ephemeral config dir is materialized on this path (_config_dir stays
    None), and the expiry is recorded anyway."""
    (tmp_path / "art").mkdir()
    canonical = tmp_path / "canonical"
    _write_canonical(canonical, NOW + 7200 * 1000)
    _canonical_env(monkeypatch, canonical)
    materialized: list[Any] = []

    def _fake_refresh(now_ms: int, *, env: Any = None,
                      force: bool = False) -> ctr.RefreshOutcome:
        return ctr.RefreshOutcome(ctr.SKIPPED_FRESH)

    def _fake_materialize() -> Path:
        materialized.append(True)
        raise AssertionError("native resume must not copy a fresh config dir")

    monkeypatch.setattr(claude_code, "refresh_canonical_token", _fake_refresh)
    monkeypatch.setattr(
        claude_code, "materialize_fresh_config_dir", _fake_materialize)

    log = InMemoryEventLog()
    inv = _inv(tmp_path, log)
    inv.transcript_dir.mkdir(parents=True, exist_ok=True)
    t = Transcript(inv.transcript_dir / "transcript.jsonl")
    t.append(Message(role="user", content=(TextBlock(text="go"),)))
    t.append(Message(role="assistant", content=(TextBlock(text="hi"),)))
    t.append(Message(role="user", content=(TextBlock(text="keep going"),)))

    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=_complete_step_client)
    events = [ev async for ev in runner.run(inv)]

    assert materialized == []                               # native path: no config copy
    assert events[-1]["type"] == "agent_result"            # no exception swallowed
    assert runner._spawn_token_expiry == NOW + 7200 * 1000


@pytest.mark.anyio
async def test_torn_canonical_file_records_none_without_derailing_the_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A torn credential file leaves the signal absent rather than raising: the
    spawn proceeds to its normal agent_result."""
    (tmp_path / "art").mkdir()
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    (canonical / ".credentials.json").write_text(
        '{"claudeAiOauth": {"expiresAt": 17', encoding="utf-8")
    _canonical_env(monkeypatch, canonical)
    _probe_seam(monkeypatch, tmp_path, ctr.RefreshOutcome(ctr.NOT_APPLICABLE))

    log = InMemoryEventLog()
    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=_complete_step_client)
    events = [ev async for ev in runner.run(_inv(tmp_path, log))]

    assert events[-1]["type"] == "agent_result"
    assert runner._spawn_token_expiry is None


def _expiry_reads(monkeypatch: pytest.MonkeyPatch,
                  values: list[int | None]) -> list[int | None]:
    """Script successive canonical_expires_at() reads and return the list of the
    ones actually taken: [0] is the spawn-time read, [1] the one the 401 path
    takes. Popping past the end raises, so an implementation that reads more
    often than scripted cannot pass silently."""
    remaining = list(values)
    taken: list[int | None] = []

    def _fake() -> int | None:
        value = remaining.pop(0)
        taken.append(value)
        return value

    monkeypatch.setattr(claude_code, "canonical_expires_at", _fake)
    return taken


@pytest.mark.anyio
async def test_rotation_race_on_native_resume_skips_the_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The incident's shape: a 401 on a native-resume spawn (no ephemeral config
    dir) whose canonical expiry moved after the spawn started. No forced refresh,
    no token_refresh row for that error, and the error stays retryable auth."""
    (tmp_path / "art").mkdir()
    seen: list[bool] = []
    materialized: list[Any] = []

    def _fake_refresh(now_ms: int, *, env: Any = None,
                      force: bool = False) -> ctr.RefreshOutcome:
        seen.append(force)
        # A forced probe here would upgrade to auth_terminal, so the category
        # and kind assertions below fail loudly if the guard does not fire.
        return (ctr.RefreshOutcome(ctr.FAILED_INVALID_GRANT) if force
                else ctr.RefreshOutcome(ctr.NOT_APPLICABLE))

    def _fake_materialize() -> Path:
        materialized.append(True)
        raise AssertionError("native resume must not copy a fresh config dir")

    monkeypatch.setattr(claude_code, "refresh_canonical_token", _fake_refresh)
    monkeypatch.setattr(
        claude_code, "materialize_fresh_config_dir", _fake_materialize)
    reads = _expiry_reads(monkeypatch, [NOW + 3600 * 1000, NOW + 7200 * 1000])

    log = InMemoryEventLog()
    inv = _inv(tmp_path, log)
    inv.transcript_dir.mkdir(parents=True, exist_ok=True)
    t = Transcript(inv.transcript_dir / "transcript.jsonl")
    t.append(Message(role="user", content=(TextBlock(text="go"),)))
    t.append(Message(role="assistant", content=(TextBlock(text="hi"),)))
    t.append(Message(role="user", content=(TextBlock(text="keep going"),)))

    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5",
        client_factory=lambda o: FakeClaudeSDKClient(
            o, scripted_messages=[assistant(text(PROSE_401)), result()]))
    events = [ev async for ev in runner.run(inv)]

    assert materialized == []                      # native path: config_dir stays None
    assert seen == [False]                         # pre-spawn refresh only, no probe
    assert reads == [NOW + 3600 * 1000, NOW + 7200 * 1000]
    errs = [e for e in events if e["type"] == "error"]
    assert [e["payload"]["category"] for e in errs] == ["auth"]
    assert errs[0]["payload"]["kind"] == "token_rotated"
    refreshes = [e for e in log.read() if e["type"] == "token_refresh"]
    assert [e["payload"] for e in refreshes] == [{"outcome": "not_applicable"}]


@pytest.mark.anyio
async def test_rotation_race_on_the_text_classified_401(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The classifier fall-through carries no token_expires_at in its detail, so
    the guard cannot be reading the error detail to reach its verdict."""
    (tmp_path / "art").mkdir()
    seen = _probe_seam(
        monkeypatch, tmp_path, ctr.RefreshOutcome(ctr.FAILED_INVALID_GRANT))
    _expiry_reads(monkeypatch, [NOW + 3600 * 1000, NOW + 3600 * 1000 + 9_000])

    log = InMemoryEventLog()
    events = [ev async for ev in _run_scripted(
        tmp_path, log, [assistant(text(PROSE_401)), result()])]

    assert seen == [False]
    errs = [e for e in events if e["type"] == "error"]
    assert [e["payload"]["category"] for e in errs] == ["auth"]
    assert errs[0]["payload"]["kind"] == "token_rotated"
    assert errs[0]["payload"]["detail"]["last_agent_text"] == PROSE_401
    assert "token_expires_at" not in errs[0]["payload"]["detail"]
    refreshes = [e for e in log.read() if e["type"] == "token_refresh"]
    assert [e["payload"] for e in refreshes] == [{"outcome": "not_applicable"}]


@pytest.mark.anyio
async def test_rotation_race_on_the_termination_branch_replaces_http_401(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The structural branch labels the error http_401 before settling; a race
    replaces that kind."""
    (tmp_path / "art").mkdir()
    seen = _probe_seam(
        monkeypatch, tmp_path, ctr.RefreshOutcome(ctr.FAILED_INVALID_GRANT))
    _expiry_reads(monkeypatch, [NOW + 3600 * 1000, NOW + 7200 * 1000])

    log = InMemoryEventLog()
    events = [ev async for ev in _run_scripted(tmp_path, log, [
        assistant(text("Session ended.")),
        result(is_error=True, api_error_status=401)])]

    assert seen == [False]
    errs = [e for e in events if e["type"] == "error"]
    assert [e["payload"]["category"] for e in errs] == ["auth"]
    assert errs[0]["payload"]["kind"] == "token_rotated"
    assert errs[0]["payload"]["detail"]["api_error_status"] == 401
    refreshes = [e for e in log.read() if e["type"] == "token_refresh"]
    assert [e["payload"] for e in refreshes] == [{"outcome": "not_applicable"}]


@pytest.mark.anyio
async def test_unmoved_expiry_still_probes_and_upgrades_a_dead_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Equal expiries mean no peer rotated: the forced probe runs as today and a
    dead grant still becomes terminal."""
    (tmp_path / "art").mkdir()
    seen = _probe_seam(
        monkeypatch, tmp_path, ctr.RefreshOutcome(ctr.FAILED_INVALID_GRANT))
    _expiry_reads(monkeypatch, [NOW + 3600 * 1000, NOW + 3600 * 1000])

    log = InMemoryEventLog()
    events = [ev async for ev in _run_scripted(
        tmp_path, log, [assistant(text(PROSE_401)), result()])]

    assert seen == [False, True]
    errs = [e for e in events if e["type"] == "error"]
    assert [e["payload"]["category"] for e in errs] == ["auth_terminal"]
    assert errs[0]["payload"]["kind"] == "AuthTerminalError"
    refreshes = [e for e in log.read() if e["type"] == "token_refresh"]
    assert [e["payload"] for e in refreshes] == [
        {"outcome": "not_applicable"}, {"outcome": "failed_invalid_grant"}]


@pytest.mark.anyio
async def test_terminal_auth_prose_is_untouched_by_a_moved_expiry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A locally-detected dead credential classifies auth_terminal, so the guard
    must never reach it: the category gate returns first, leaving the 401-time
    read untaken and the kind at the class-name default."""
    (tmp_path / "art").mkdir()
    seen = _probe_seam(
        monkeypatch, tmp_path, ctr.RefreshOutcome(ctr.FAILED_INVALID_GRANT))
    reads = _expiry_reads(monkeypatch, [NOW + 3600 * 1000, NOW + 7200 * 1000])

    log = InMemoryEventLog()
    events = [ev async for ev in _run_scripted(tmp_path, log, [
        assistant(text("Invalid API key · Please run `/login`")), result()])]

    assert seen == [False]                     # category gate: no probe either way
    assert reads == [NOW + 3600 * 1000]        # only the spawn-time read happened
    errs = [e for e in events if e["type"] == "error"]
    assert [e["payload"]["category"] for e in errs] == ["auth_terminal"]
    assert errs[0]["payload"]["kind"] == "AuthTerminalError"


@pytest.mark.parametrize(
    "values",
    [[None, NOW + 7200 * 1000], [NOW + 3600 * 1000, None], [None, None]],
    ids=["spawn-unreadable", "current-unreadable", "both-unreadable"],
)
@pytest.mark.anyio
async def test_unreadable_expiry_probes_exactly_as_today(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, values: list[int | None],
) -> None:
    """An absent signal is untrustworthy, not evidence of a race: today's forced
    probe runs and the error keeps its http_401 kind."""
    (tmp_path / "art").mkdir()
    seen = _probe_seam(
        monkeypatch, tmp_path,
        ctr.RefreshOutcome(ctr.REFRESHED, expires_at=NOW + 3600 * 1000))
    _expiry_reads(monkeypatch, values)

    log = InMemoryEventLog()
    events = [ev async for ev in _run_scripted(
        tmp_path, log, [assistant(text(PROSE_401)), result()])]

    assert seen == [False, True]
    errs = [e for e in events if e["type"] == "error"]
    assert [e["payload"]["category"] for e in errs] == ["auth"]
    assert errs[0]["payload"]["kind"] == "http_401"
    refreshes = [e for e in log.read() if e["type"] == "token_refresh"]
    assert [e["payload"] for e in refreshes] == [
        {"outcome": "not_applicable"},
        {"outcome": "refreshed", "expires_at": NOW + 3600 * 1000}]
