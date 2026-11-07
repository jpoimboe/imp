"""A batched complete_step end to end on the claude runner: the sibling bash runs to
its natural end, the batched call is rejected, and the model calls it again."""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from imp.agents.runner import canonicalize_tool_name
from imp.agents.transcript import COMPLETE_STEP_BATCHED
from imp.engine.state import parse_events_jsonl
from impty import TerminalSession
from tests._live import (
    _archived_events,
    _await_change_dir,
    _bash_result_dicts,
    path_claude_skip_reason,
    sandbox_sleep_precondition_failed,
    wait_for_archived_status,
)
from tests.test_relay_hibernation_live import _WORKTREE_SRC, _seed_cc_project

_RUN_TIMEOUT = 180.0  # inside the 240s per-test backstop; a pause never reaches `completed`
_SANDBOX_SKIP = (
    "imp's bash sandbox could not start (the bash result has status "
    "sandbox_setup_failed); the writer's `sleep 5` never ran, so there was no sibling "
    "to keep running. This is a sandbox/environment failure, not a complete_step "
    "regression."
)
_NOT_BATCHED_SKIP = (
    "the model did not send bash and complete_step in one API message, so the batched "
    "path never ran. This is model non-compliance, not a complete_step regression."
)


def _seed_batching_writer(root: Path) -> None:
    _seed_cc_project(root)
    (root / "imp" / "workflows" / "imp" / "agents" / "writer.md").write_text(
        textwrap.dedent("""\
        ---
        name: writer
        description: A test agent that batches bash and complete_step.
        tools: [complete_step, bash]
        output:
          status: {type: enum, values: [Success, Error]}
        ---
        In ONE single assistant message, emit both tool calls together, in this order:
        bash(`sleep 5`) and then complete_step(status=Success). Do not wait between them.
        If complete_step is rejected, follow what the error says.
    """))
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-qm", "batching writer"],
        cwd=root, check=True,
    )


def _records(quest_dir: Path) -> list[dict[str, Any]]:
    raw = _archived_events(quest_dir)
    if raw is None:
        raw = (quest_dir / "events.jsonl").read_bytes()
    return parse_events_jsonl(raw)


def _sent_in_one_message(records: list[dict[str, Any]]) -> bool:
    # Read what the model sent, not a rejection: a rule regression must fail here, not skip.
    by_message: dict[Any, list[str]] = {}
    for r in records:
        if r.get("type") != "agent_message":
            continue
        payload = r["payload"]
        for block in payload["raw"]["content"]:
            if block.get("type") == "tool_use":
                by_message.setdefault(payload.get("message_id"), []).append(
                    canonicalize_tool_name(block["name"]))
    return any(names[:2] == ["bash", "complete_step"] for names in by_message.values())


@pytest.mark.live("claude")
def test_batched_complete_step_is_rejected_then_called_again(
    tmp_path: Path, register_quest_evidence: Callable[[Path], None],
) -> None:
    reason = path_claude_skip_reason()
    if reason is not None:
        pytest.skip(reason)
    _seed_batching_writer(tmp_path)
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    pypath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = _WORKTREE_SRC + (os.pathsep + pypath if pypath else "")
    env.setdefault("IMP_THEME", "ansi")

    s = TerminalSession(
        [sys.executable, "-m", "imp", "new", "batched-complete"],
        cwd=str(tmp_path), env=env, cols=80, rows=24,
    )
    try:
        quest_dir = _await_change_dir(tmp_path, timeout=30.0)
        register_quest_evidence(quest_dir)
        status = wait_for_archived_status(
            tmp_path, quest_dir.name, "completed", timeout=_RUN_TIMEOUT)
    finally:
        s.close()

    if sandbox_sleep_precondition_failed(quest_dir / "events.jsonl") is not None:
        pytest.skip(_SANDBOX_SKIP)
    records = _records(quest_dir)
    if not _sent_in_one_message(records):
        pytest.skip(_NOT_BATCHED_SKIP)

    assert status == "completed"
    bash_ids = {r["payload"]["id"] for r in records if r.get("type") == "tool_call"
                and r["payload"].get("name") == "bash"}
    bash_results = [
        result for r in records
        if r.get("type") == "tool_result" and r["payload"].get("id") in bash_ids
        for result in _bash_result_dicts(r["payload"].get("content"))]
    assert any(result.get("returncode") == 0 for result in bash_results)
    rejected_at = next(
        (i for i, r in enumerate(records) if r.get("type") == "tool_result"
         and r["payload"].get("content") == COMPLETE_STEP_BATCHED
         and r["payload"].get("error") is True), None)
    assert rejected_at is not None, "the batched complete_step was never rejected"
    assert any(
        r.get("type") == "tool_call" and r["payload"].get("name") == "complete_step"
        for r in records[rejected_at + 1:])
    agent_results = [r["payload"] for r in records if r.get("type") == "agent_result"]
    assert agent_results[-1]["status"] == "Success"
