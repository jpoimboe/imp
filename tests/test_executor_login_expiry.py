"""Run-start advisory when the saved Claude login is near the end of its window.

Modelled on tests/test_executor_outer_imp.py — the sibling once-per-run advisory.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from imp.engine.executor import RunResult, run_quest
from tests.test_executor import _register_fake, _seed_minimal_workflow

_DAY_MS = 24 * 60 * 60 * 1000


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _seed_login(base: Path, monkeypatch: pytest.MonkeyPatch, *,
                remaining_ms: int) -> None:
    base.mkdir(parents=True, exist_ok=True)
    now_ms = int(time.time() * 1000)
    (base / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {
            "accessToken": "a",
            "refreshToken": "r",
            "expiresAt": now_ms + 8 * 60 * 60 * 1000,
            "refreshTokenExpiresAt": now_ms + remaining_ms,
        }}),
        encoding="utf-8")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(base))
    # conftest stubs ANTHROPIC_API_KEY for every non-live test; the env guard would
    # otherwise suppress the read outright.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)


def _expiry_events(events: list[dict]) -> list[dict]:
    return [e for e in events
            if e["type"] == "warning"
            and e.get("payload", {}).get("source") == "claude_login_expiry"]


@pytest.mark.anyio
@pytest.mark.bare_sandbox
async def test_near_expiry_login_warns_once_at_run_start(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    quest, _, _ = _seed_minimal_workflow(tmp_path)
    # +6h of slack: the floor stays 2 whole days however long the run takes.
    _seed_login(tmp_path / "claude-home", monkeypatch,
                remaining_ms=2 * _DAY_MS + 6 * 60 * 60 * 1000)
    _register_fake({"status": "Success"})

    events: list[dict] = []
    result = await run_quest(quest, on_event=events.append)

    assert result is RunResult.COMPLETED
    warnings = _expiry_events(events)
    assert len(warnings) == 1
    assert warnings[0]["payload"]["message"] == (
        "Claude login expires in 2 days — run `claude` then `/login` to renew")
    assert warnings[0]["payload"]["detail"] == {}


@pytest.mark.anyio
@pytest.mark.bare_sandbox
async def test_second_run_quest_entry_in_the_same_process_stays_silent(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The daemon hold loop re-enters run_quest after every user reply; the per-process
    gate ensures the advisory is emitted at most once per process."""
    quest, _, _ = _seed_minimal_workflow(tmp_path)
    _seed_login(tmp_path / "claude-home", monkeypatch,
                remaining_ms=2 * _DAY_MS + 6 * 60 * 60 * 1000)
    _register_fake({"status": "Success"})

    events: list[dict] = []
    await run_quest(quest, on_event=events.append)
    assert len(_expiry_events(events)) == 1

    from imp.engine.archive import read as archive_read
    archive_read.rehydrate_quest(quest.control_root, quest.id)  # home reaped on done
    before_second = len(events)
    await run_quest(quest, on_event=events.append)              # same process, 2nd entry

    # Liveness: the second entry really ran, so the count below is a gated
    # emission and not a run that bailed before the run-start block.
    assert len(events) > before_second
    assert len(_expiry_events(events)) == 1


@pytest.mark.anyio
@pytest.mark.bare_sandbox
async def test_distant_expiry_emits_nothing(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    quest, _, _ = _seed_minimal_workflow(tmp_path)
    _seed_login(tmp_path / "claude-home", monkeypatch, remaining_ms=10 * _DAY_MS)
    _register_fake({"status": "Success"})

    events: list[dict] = []
    result = await run_quest(quest, on_event=events.append)

    assert result is RunResult.COMPLETED
    assert _expiry_events(events) == []
