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
from imp.providers import claude
from imp.providers import claude_token_refresh as ctr
from imp.providers.claude import ClaudeAgentRunner
from tests.cc_fakes import FakeClaudeSDKClient, assistant, chrome, result, text, tool_use

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
        provider_name="claude", model_name="claude-sonnet-4-5",
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

    def _fake_refresh(now_ms: int, *, env: Any = None, force: bool = False,
                      spawn_expires_at: int | None = None) -> ctr.RefreshOutcome:
        order.append("refresh")
        return ctr.RefreshOutcome(ctr.REFRESHED, expires_at=NOW + 3600 * 1000)

    def _fake_materialize() -> Path:
        order.append("materialize")
        cfg = tmp_path / "cfg"
        (cfg / "projects").mkdir(parents=True, exist_ok=True)
        return cfg

    monkeypatch.setattr(claude, "refresh_canonical_token", _fake_refresh)
    monkeypatch.setattr(
        claude, "materialize_fresh_config_dir", _fake_materialize)

    log = InMemoryEventLog()
    runner = ClaudeAgentRunner(
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

    def _fake_refresh(now_ms: int, *, env: Any = None, force: bool = False,
                      spawn_expires_at: int | None = None) -> ctr.RefreshOutcome:
        calls.append("refresh")
        return ctr.RefreshOutcome(ctr.SKIPPED_FRESH)

    def _fake_materialize() -> Path:
        calls.append("materialize")
        raise AssertionError("native resume must not copy a fresh config dir")

    monkeypatch.setattr(claude, "refresh_canonical_token", _fake_refresh)
    monkeypatch.setattr(
        claude, "materialize_fresh_config_dir", _fake_materialize)

    log = InMemoryEventLog()
    inv = _inv(tmp_path, log)
    inv.transcript_dir.mkdir(parents=True, exist_ok=True)
    # Seed a resume-eligible transcript: assistant text tail + trailing plain-text
    # user turn drives the native-resume disposition (config_dir stays None).
    t = Transcript(inv.transcript_dir / "transcript.jsonl")
    t.append(Message(role="user", content=(TextBlock(text="go"),)))
    t.append(Message(role="assistant", content=(TextBlock(text="hi"),)))
    t.append(Message(role="user", content=(TextBlock(text="keep going"),)))

    runner = ClaudeAgentRunner(
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

    def _dead(now_ms: int, *, env: Any = None, force: bool = False,
              spawn_expires_at: int | None = None) -> ctr.RefreshOutcome:
        return ctr.RefreshOutcome(ctr.FAILED_INVALID_GRANT)

    def _fake_materialize() -> Path:
        raise AssertionError("dead token must short-circuit before config copy")

    monkeypatch.setattr(claude, "refresh_canonical_token", _dead)
    monkeypatch.setattr(
        claude, "materialize_fresh_config_dir", _fake_materialize)

    log = InMemoryEventLog()
    runner = ClaudeAgentRunner(
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
) -> list[tuple[bool, int | None]]:
    """Seam: forced calls get forced_outcome, unforced get NOT_APPLICABLE; returns the
    (force, spawn_expires_at) pair of every call, so the wiring is asserted on the value."""
    calls: list[tuple[bool, int | None]] = []

    def _fake_refresh(now_ms: int, *, env: Any = None, force: bool = False,
                      spawn_expires_at: int | None = None) -> ctr.RefreshOutcome:
        calls.append((force, spawn_expires_at))
        return forced_outcome if force else ctr.RefreshOutcome(ctr.NOT_APPLICABLE)

    def _fake_materialize() -> Path:
        cfg = tmp_path / "cfg"
        (cfg / "projects").mkdir(parents=True, exist_ok=True)
        return cfg

    monkeypatch.setattr(claude, "refresh_canonical_token", _fake_refresh)
    monkeypatch.setattr(
        claude, "materialize_fresh_config_dir", _fake_materialize)
    return calls


def _run_scripted(tmp_path: Path, log: InMemoryEventLog,
                  scripted: list[Any]) -> Any:
    runner = ClaudeAgentRunner(
        model_name="claude-sonnet-4-5",
        client_factory=lambda o: FakeClaudeSDKClient(o, scripted_messages=scripted))
    return runner.run(_inv(tmp_path, log))


@pytest.mark.anyio
async def test_dead_grant_probe_upgrades_the_auth_error_to_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "art").mkdir()
    calls = _probe_seam(
        monkeypatch, tmp_path, ctr.RefreshOutcome(ctr.FAILED_INVALID_GRANT))

    log = InMemoryEventLog()
    events = [ev async for ev in _run_scripted(
        tmp_path, log, [chrome(PROSE_401), result()])]

    # Pre-spawn refresh unforced and with no spawn token; the terminality probe forced.
    assert calls == [(False, None), (True, None)]
    errs = [e for e in events if e["type"] == "error"]
    assert [e["payload"]["category"] for e in errs] == ["auth_terminal"]
    assert errs[0]["payload"]["kind"] == "AuthTerminalError"
    assert errs[0]["payload"]["message"] == (
        "claude: OAuth refresh token is dead; run `claude login`")
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
    calls = _probe_seam(
        monkeypatch, tmp_path,
        ctr.RefreshOutcome(ctr.REFRESHED, expires_at=NOW + 3600 * 1000))

    log = InMemoryEventLog()
    events = [ev async for ev in _run_scripted(
        tmp_path, log, [chrome(PROSE_401), result()])]

    assert calls == [(False, None), (True, None)]
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
    calls = _probe_seam(
        monkeypatch, tmp_path, ctr.RefreshOutcome(ctr.FAILED_INVALID_GRANT))

    log = InMemoryEventLog()
    # api_error_status labels the termination 'auth', exercising _termination_from_result.
    events = [ev async for ev in _run_scripted(tmp_path, log, [
        assistant(text("Session ended.")),
        result(is_error=True, api_error_status=401)])]

    assert calls == [(False, None), (True, None)]
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
    calls = _probe_seam(
        monkeypatch, tmp_path, ctr.RefreshOutcome(ctr.FAILED_INVALID_GRANT))

    log = InMemoryEventLog()
    events = [ev async for ev in _run_scripted(
        tmp_path, log, [chrome("API Error: 500 upstream is down"),
                        result()])]

    # Only the unforced pre-spawn refresh ran: no probe, no extra journal row.
    assert calls == [(False, None)]
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

    def _fake_refresh(now_ms: int, *, env: Any = None, force: bool = False,
                      spawn_expires_at: int | None = None) -> ctr.RefreshOutcome:
        order.append("refresh")
        _write_canonical(canonical, NOW + 3600 * 1000)  # the refresh rotates it
        return ctr.RefreshOutcome(ctr.REFRESHED, expires_at=NOW + 3600 * 1000)

    def _fake_materialize() -> Path:
        order.append("materialize")
        cfg = tmp_path / "cfg"
        (cfg / "projects").mkdir(parents=True, exist_ok=True)
        return cfg

    monkeypatch.setattr(claude, "refresh_canonical_token", _fake_refresh)
    monkeypatch.setattr(
        claude, "materialize_fresh_config_dir", _fake_materialize)

    log = InMemoryEventLog()
    runner = ClaudeAgentRunner(
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

    def _fake_refresh(now_ms: int, *, env: Any = None, force: bool = False,
                      spawn_expires_at: int | None = None) -> ctr.RefreshOutcome:
        return ctr.RefreshOutcome(ctr.SKIPPED_FRESH)

    def _fake_materialize() -> Path:
        materialized.append(True)
        raise AssertionError("native resume must not copy a fresh config dir")

    monkeypatch.setattr(claude, "refresh_canonical_token", _fake_refresh)
    monkeypatch.setattr(
        claude, "materialize_fresh_config_dir", _fake_materialize)

    log = InMemoryEventLog()
    inv = _inv(tmp_path, log)
    inv.transcript_dir.mkdir(parents=True, exist_ok=True)
    t = Transcript(inv.transcript_dir / "transcript.jsonl")
    t.append(Message(role="user", content=(TextBlock(text="go"),)))
    t.append(Message(role="assistant", content=(TextBlock(text="hi"),)))
    t.append(Message(role="user", content=(TextBlock(text="keep going"),)))

    runner = ClaudeAgentRunner(
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
    runner = ClaudeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=_complete_step_client)
    events = [ev async for ev in runner.run(_inv(tmp_path, log))]

    assert events[-1]["type"] == "agent_result"
    assert runner._spawn_token_expiry is None


def _expiry_reads(monkeypatch: pytest.MonkeyPatch,
                  values: list[int | None]) -> list[int | None]:
    """Script successive canonical_expires_at() reads and return the ones actually
    taken. Only the spawn-time read remains; popping past the end raises, so a 401
    path that reads the canonical expiry again cannot pass silently."""
    remaining = list(values)
    taken: list[int | None] = []

    def _fake() -> int | None:
        value = remaining.pop(0)
        taken.append(value)
        return value

    monkeypatch.setattr(claude, "canonical_expires_at", _fake)
    return taken


@pytest.mark.anyio
async def test_rotation_race_on_native_resume_stays_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The incident's shape: a 401 on a native-resume spawn (no ephemeral config
    dir) whose canonical expiry moved after the spawn started. The forced probe
    runs, reports the race from under the lock, and the error stays retryable auth."""
    (tmp_path / "art").mkdir()
    calls: list[tuple[bool, int | None]] = []
    materialized: list[Any] = []

    def _fake_refresh(now_ms: int, *, env: Any = None, force: bool = False,
                      spawn_expires_at: int | None = None) -> ctr.RefreshOutcome:
        calls.append((force, spawn_expires_at))
        # A dead-grant verdict here would upgrade to auth_terminal, so the category
        # and kind assertions below fail loudly if SKIPPED_ROTATED is mishandled.
        return (ctr.RefreshOutcome(ctr.SKIPPED_ROTATED) if force
                else ctr.RefreshOutcome(ctr.NOT_APPLICABLE))

    def _fake_materialize() -> Path:
        materialized.append(True)
        raise AssertionError("native resume must not copy a fresh config dir")

    monkeypatch.setattr(claude, "refresh_canonical_token", _fake_refresh)
    monkeypatch.setattr(
        claude, "materialize_fresh_config_dir", _fake_materialize)
    reads = _expiry_reads(monkeypatch, [NOW + 3600 * 1000])

    log = InMemoryEventLog()
    inv = _inv(tmp_path, log)
    inv.transcript_dir.mkdir(parents=True, exist_ok=True)
    t = Transcript(inv.transcript_dir / "transcript.jsonl")
    t.append(Message(role="user", content=(TextBlock(text="go"),)))
    t.append(Message(role="assistant", content=(TextBlock(text="hi"),)))
    t.append(Message(role="user", content=(TextBlock(text="keep going"),)))

    runner = ClaudeAgentRunner(
        model_name="claude-sonnet-4-5",
        client_factory=lambda o: FakeClaudeSDKClient(
            o, scripted_messages=[chrome(PROSE_401), result()]))
    events = [ev async for ev in runner.run(inv)]

    assert materialized == []                      # native path: config_dir stays None
    assert calls == [(False, None), (True, NOW + 3600 * 1000)]
    assert reads == [NOW + 3600 * 1000]            # only the spawn-time read happened
    errs = [e for e in events if e["type"] == "error"]
    assert [e["payload"]["category"] for e in errs] == ["auth"]
    assert errs[0]["payload"]["kind"] == "token_rotated"
    refreshes = [e for e in log.read() if e["type"] == "token_refresh"]
    assert [e["payload"] for e in refreshes] == [
        {"outcome": "not_applicable"}, {"outcome": "skipped_rotated"}]


@pytest.mark.anyio
async def test_rotation_race_on_the_text_classified_401(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The classifier fall-through carries no token_expires_at in its detail: the
    verdict comes from the probe's outcome, not from the error detail."""
    (tmp_path / "art").mkdir()
    calls = _probe_seam(
        monkeypatch, tmp_path, ctr.RefreshOutcome(ctr.SKIPPED_ROTATED))
    _expiry_reads(monkeypatch, [NOW + 3600 * 1000])

    log = InMemoryEventLog()
    events = [ev async for ev in _run_scripted(
        tmp_path, log, [chrome(PROSE_401), result()])]

    assert calls == [(False, None), (True, NOW + 3600 * 1000)]
    errs = [e for e in events if e["type"] == "error"]
    assert [e["payload"]["category"] for e in errs] == ["auth"]
    assert errs[0]["payload"]["kind"] == "token_rotated"
    assert errs[0]["payload"]["detail"]["last_agent_text"] == PROSE_401
    assert "token_expires_at" not in errs[0]["payload"]["detail"]
    refreshes = [e for e in log.read() if e["type"] == "token_refresh"]
    assert [e["payload"] for e in refreshes] == [
        {"outcome": "not_applicable"}, {"outcome": "skipped_rotated"}]


@pytest.mark.anyio
async def test_rotation_race_on_the_termination_branch_replaces_http_401(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The structural branch labels the error http_401 before settling; a race
    replaces that kind."""
    (tmp_path / "art").mkdir()
    calls = _probe_seam(
        monkeypatch, tmp_path, ctr.RefreshOutcome(ctr.SKIPPED_ROTATED))
    _expiry_reads(monkeypatch, [NOW + 3600 * 1000])

    log = InMemoryEventLog()
    events = [ev async for ev in _run_scripted(tmp_path, log, [
        assistant(text("Session ended.")),
        result(is_error=True, api_error_status=401)])]

    assert calls == [(False, None), (True, NOW + 3600 * 1000)]
    errs = [e for e in events if e["type"] == "error"]
    assert [e["payload"]["category"] for e in errs] == ["auth"]
    assert errs[0]["payload"]["kind"] == "token_rotated"
    assert errs[0]["payload"]["detail"]["api_error_status"] == 401
    refreshes = [e for e in log.read() if e["type"] == "token_refresh"]
    assert [e["payload"] for e in refreshes] == [
        {"outcome": "not_applicable"}, {"outcome": "skipped_rotated"}]


@pytest.mark.anyio
async def test_the_probe_gets_the_spawn_expiry_and_still_upgrades_a_dead_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The spawn expiry rides along to the probe, which compares it under the lock;
    with no peer rotation, a dead grant still becomes terminal."""
    (tmp_path / "art").mkdir()
    calls = _probe_seam(
        monkeypatch, tmp_path, ctr.RefreshOutcome(ctr.FAILED_INVALID_GRANT))
    _expiry_reads(monkeypatch, [NOW + 3600 * 1000])

    log = InMemoryEventLog()
    events = [ev async for ev in _run_scripted(
        tmp_path, log, [chrome(PROSE_401), result()])]

    assert calls == [(False, None), (True, NOW + 3600 * 1000)]
    errs = [e for e in events if e["type"] == "error"]
    assert [e["payload"]["category"] for e in errs] == ["auth_terminal"]
    assert errs[0]["payload"]["kind"] == "AuthTerminalError"
    refreshes = [e for e in log.read() if e["type"] == "token_refresh"]
    assert [e["payload"] for e in refreshes] == [
        {"outcome": "not_applicable"}, {"outcome": "failed_invalid_grant"}]


@pytest.mark.anyio
async def test_terminal_auth_prose_never_reaches_the_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A locally-detected dead credential classifies auth_terminal, so the category
    gate returns before any forced probe: no second refresh call, no second journal
    row, and the kind stays at the class-name default."""
    (tmp_path / "art").mkdir()
    calls = _probe_seam(
        monkeypatch, tmp_path, ctr.RefreshOutcome(ctr.FAILED_INVALID_GRANT))
    _expiry_reads(monkeypatch, [NOW + 3600 * 1000])

    log = InMemoryEventLog()
    events = [ev async for ev in _run_scripted(tmp_path, log, [
        chrome("Invalid API key · Please run `/login`"), result()])]

    assert calls == [(False, None)]            # category gate: no probe either way
    errs = [e for e in events if e["type"] == "error"]
    assert [e["payload"]["category"] for e in errs] == ["auth_terminal"]
    assert errs[0]["payload"]["kind"] == "AuthTerminalError"
    refreshes = [e for e in log.read() if e["type"] == "token_refresh"]
    assert [e["payload"] for e in refreshes] == [{"outcome": "not_applicable"}]


@pytest.mark.anyio
async def test_an_unreadable_spawn_expiry_passes_none_and_probes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An absent spawn-time signal is untrustworthy, not evidence of a race: None
    goes to the probe, which runs, and the error keeps its http_401 kind."""
    (tmp_path / "art").mkdir()
    calls = _probe_seam(
        monkeypatch, tmp_path,
        ctr.RefreshOutcome(ctr.REFRESHED, expires_at=NOW + 3600 * 1000))
    _expiry_reads(monkeypatch, [None])

    log = InMemoryEventLog()
    events = [ev async for ev in _run_scripted(
        tmp_path, log, [chrome(PROSE_401), result()])]

    assert calls == [(False, None), (True, None)]
    errs = [e for e in events if e["type"] == "error"]
    assert [e["payload"]["category"] for e in errs] == ["auth"]
    assert errs[0]["payload"]["kind"] == "http_401"
    refreshes = [e for e in log.read() if e["type"] == "token_refresh"]
    assert [e["payload"] for e in refreshes] == [
        {"outcome": "not_applicable"},
        {"outcome": "refreshed", "expires_at": NOW + 3600 * 1000}]


INCIDENT_AUTH_PROSE = (
    "Failed to authenticate: OAuth token revoked. Please log in again or contact "
    "your administrator.")


@pytest.mark.anyio
async def test_server_401_under_auth_prose_probes_and_sees_the_rotation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The out-of-band 401 outranks the prose, so the probe runs and a peer
    rotation retries fast instead of pausing for a re-login."""
    (tmp_path / "art").mkdir()
    calls = _probe_seam(
        monkeypatch, tmp_path, ctr.RefreshOutcome(ctr.SKIPPED_ROTATED))

    log = InMemoryEventLog()
    events = [ev async for ev in _run_scripted(tmp_path, log, [
        chrome(INCIDENT_AUTH_PROSE), result(is_error=True, api_error_status=401)])]

    assert calls == [(False, None), (True, None)]
    errs = [e for e in events if e["type"] == "error"]
    assert [e["payload"]["category"] for e in errs] == ["auth"]
    assert errs[0]["payload"]["kind"] == "token_rotated"
    assert errs[0]["payload"]["detail"]["api_error_status"] == 401
    assert errs[0]["payload"]["message"] == f"claude CLI: {INCIDENT_AUTH_PROSE}"


@pytest.mark.anyio
async def test_server_401_under_auth_prose_with_a_dead_grant_pauses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "art").mkdir()
    calls = _probe_seam(
        monkeypatch, tmp_path, ctr.RefreshOutcome(ctr.FAILED_INVALID_GRANT))

    log = InMemoryEventLog()
    events = [ev async for ev in _run_scripted(tmp_path, log, [
        chrome(INCIDENT_AUTH_PROSE), result(is_error=True, api_error_status=401)])]

    # The probe ran: the terminal verdict comes from the dead grant, not the prose.
    assert calls == [(False, None), (True, None)]
    errs = [e for e in events if e["type"] == "error"]
    assert [e["payload"]["category"] for e in errs] == ["auth_terminal"]
    assert errs[0]["payload"]["kind"] == "AuthTerminalError"
    assert errs[0]["payload"]["message"] == (
        "claude: OAuth refresh token is dead; run `claude login`")


@pytest.mark.anyio
async def test_server_401_under_auth_prose_with_a_live_grant_stays_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "art").mkdir()
    calls = _probe_seam(
        monkeypatch, tmp_path,
        ctr.RefreshOutcome(ctr.REFRESHED, expires_at=NOW + 3600 * 1000))

    log = InMemoryEventLog()
    events = [ev async for ev in _run_scripted(tmp_path, log, [
        chrome(INCIDENT_AUTH_PROSE), result(is_error=True, api_error_status=401)])]

    assert calls == [(False, None), (True, None)]
    errs = [e for e in events if e["type"] == "error"]
    assert [e["payload"]["category"] for e in errs] == ["auth"]
    assert errs[0]["payload"]["kind"] == "http_401"
