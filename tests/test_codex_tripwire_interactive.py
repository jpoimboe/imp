"""AC #18: interactive mode default-revert + explicit-keep logging.

Drives `CodexAgentRunner.run()` end-to-end with a scripted FakeCodex stream
that contains a `file_change` native item (which Task 25 flagged via
`proc_state["native_violation"]`). Two scenarios:

  1. Interactive + `user_input_fn` returns anything except "keep" (or
     raises) -> bytes reverted (snapshot restored), `native_tool_violation`
     error yielded, decision is logged as "revert".
  2. Interactive + `user_input_fn` returns "keep" -> snapshot is NOT
     restored, the decision is logged as "keep" **before** the
     `native_tool_violation` error is yielded (order-sensitive: the log
     entry must precede the error event so a post-mortem reader sees the
     keep before any subsequent retention is acknowledged).

Both scenarios still terminate the run with the fail-closed error event --
"keep" only governs whether bytes are reverted, not whether the run
continues.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from imp.agents.runner import AgentInvocation, ChangeContext
from imp.engine.state import InMemoryEventLog
from imp.engine.types import (
    AgentDef,
    FsRule,
    OutputField,
    OutputSchema,
    SourceLoc,
)
from imp.providers.codex_runner import CodexAgentRunner
from tests.codex_fakes import (
    FakeCodex,
    agent_message,
    file_change,
    thread_started,
    turn_completed,
)

_SRC = SourceLoc(file=Path("a.md"), line=1)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _git_init(p: Path) -> None:
    subprocess.run(["git", "init", "-q", str(p)], check=True)
    subprocess.run(
        ["git", "-C", str(p), "config", "user.email", "t@t.t"], check=True)
    subprocess.run(
        ["git", "-C", str(p), "config", "user.name", "T"], check=True)


def _agent_def() -> AgentDef:
    return AgentDef(
        name="t",
        description="",
        tools=frozenset({"complete_step"}),
        subagents=(),
        output_schema=OutputSchema(
            fields=(OutputField(name="answer", type="string", required=True),),
            source=_SRC,
        ),
        prompt_template_source="",
        source_path=Path("a.md"),
        source=_SRC,
        tier=None,
    )


def _invocation(
    tmp_path: Path, *, interactive: bool, user_input_fn: object,
    event_log: object,
) -> AgentInvocation:
    return AgentInvocation(
        agent_def=_agent_def(),
        provider_name="codex",
        model_name="gpt-5.5",
        system_prompt="you are a test agent",
        initial_messages=(),
        change_context=ChangeContext(
            slug="s", title="t", project_root=tmp_path,
            workflow_dir=tmp_path, id="c1"),
        interactive=interactive,
        transcript_dir=tmp_path / "tx",
        fs_permissions=(
            FsRule(operations=("read", "write"), paths=("**",),
                   mode="allow", source=_SRC),
        ),
        events_path=tmp_path / "events.jsonl",
        agent_id="step:t",
        user_input_fn=user_input_fn,
        event_log=event_log,
    )


def _seed(tmp_path: Path) -> Path:
    """Set up a git repo with one tracked file and a canonical auth blob."""
    _git_init(tmp_path)
    tracked = tmp_path / "file.txt"
    tracked.write_text("entry-contents\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-q",
                    "-m", "seed"], check=True)
    canonical = tmp_path / "auth.json"
    canonical.write_text('{"auth_mode": "chatgpt", "tokens": {}}')
    return tracked


def _scripted_native_edit() -> list[list[object]]:
    """A turn where codex emits a `file_change` native item -- soft tripwire
    fires; `proc_state["native_violation"]` is set in Task 25."""
    return [[
        thread_started("thr-x"),
        agent_message("editing file directly"),
        file_change("file.txt"),
        turn_completed(input_tokens=10, output_tokens=5),
    ]]


@pytest.mark.anyio
async def test_interactive_default_reverts_bytes(tmp_path: Path) -> None:
    """Interactive + empty/anything-but-keep answer -> default revert: the
    tracked file is restored to its entry contents and an error event is
    yielded. The decision log entry says "revert"."""
    tracked = _seed(tmp_path)
    elog = InMemoryEventLog()
    (tmp_path / "tx").mkdir()

    # Anything not equal to "keep" -> revert. Use empty string (the most
    # common "user just hit enter" default).
    def _user_input_fn(_prompt: str) -> str:
        return ""

    runner = CodexAgentRunner(
        model_name="gpt-5.5",
        client_factory=lambda options=None: FakeCodex(
            scripted_turns=_scripted_native_edit(),
            on_turn_start=lambda: tracked.write_text("disobedient-edit\n")),
        user_input_fn=_user_input_fn,
    )
    runner._canonical_auth_override = tmp_path / "auth.json"

    inv = _invocation(tmp_path, interactive=True,
                      user_input_fn=_user_input_fn, event_log=elog)

    events = []
    async for ev in runner.run(inv):
        events.append(ev)

    # File reverted.
    assert tracked.read_text() == "entry-contents\n"
    # The error event is yielded last.
    last = events[-1]
    assert last["type"] == "error"
    assert last["payload"]["kind"] == "native_tool_violation"
    assert last["payload"]["detail"]["kept_bytes"] is False
    # Decision logged as "revert" in event log.
    decisions = [e for e in elog.read()
                 if e["type"] == "runner_warning"
                 and (e["payload"] or {}).get("decision") in {"keep", "revert"}]
    assert len(decisions) == 1
    assert decisions[0]["payload"]["decision"] == "revert"


@pytest.mark.anyio
async def test_interactive_keep_retains_bytes_and_logs_before_retention(
        tmp_path: Path) -> None:
    """Interactive + answer="keep" -> bytes retained (no snapshot restore),
    and the "keep" decision is logged BEFORE the `native_tool_violation`
    error event (AC #18: keep is "explicitly logged before bytes are
    retained" -- order matters for post-mortem readers)."""
    tracked = _seed(tmp_path)
    elog = InMemoryEventLog()
    (tmp_path / "tx").mkdir()

    def _user_input_fn(_prompt: str) -> str:
        return "keep"

    runner = CodexAgentRunner(
        model_name="gpt-5.5",
        client_factory=lambda options=None: FakeCodex(
            scripted_turns=_scripted_native_edit(),
            on_turn_start=lambda: tracked.write_text("disobedient-edit\n")),
        user_input_fn=_user_input_fn,
    )
    runner._canonical_auth_override = tmp_path / "auth.json"

    inv = _invocation(tmp_path, interactive=True,
                      user_input_fn=_user_input_fn, event_log=elog)

    events = []
    async for ev in runner.run(inv):
        events.append(ev)

    # Bytes retained -- file NOT reverted.
    assert tracked.read_text() == "disobedient-edit\n"
    # The error event is yielded last; detail.kept_bytes is True.
    last = events[-1]
    assert last["type"] == "error"
    assert last["payload"]["kind"] == "native_tool_violation"
    assert last["payload"]["detail"]["kept_bytes"] is True
    # AC #18 ordering: the "keep" decision is *persisted* to the event log
    # before the run terminates.
    persisted = list(elog.read())
    keep_logs = [e for e in persisted
                 if e["type"] == "runner_warning"
                 and (e["payload"] or {}).get("decision") == "keep"]
    assert len(keep_logs) == 1, (
        "exactly one keep-decision log entry must exist in persisted "
        "event log before the run returns")
    # The soft-tripwire `runner_warning` from Task 25 (no `decision` key)
    # must precede the decision log -- the decision is recorded *after*
    # the violation is observed.
    soft_warn_idx = next(
        i for i, e in enumerate(persisted)
        if e["type"] == "runner_warning"
        and "native file_change detected"
        in str((e["payload"] or {}).get("message", ""))
    )
    decision_idx = persisted.index(keep_logs[0])
    assert soft_warn_idx < decision_idx
