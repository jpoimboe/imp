"""A scripted parent can dispatch a sub_agent tool call.

The scripted runner routes each turn's tool_uses through the shared
dispatch_turn_tool_uses partition (like the real providers), so a `sub_agent`
tu spawns the child via ctx.spawn_subagent instead of tripping the
task-routing RuntimeError in dispatch_one_tool_use. This is what lets a
scripted driver dispatch a real coder subagent (the coder A/B harness).
"""

from __future__ import annotations

import functools
import json
from pathlib import Path

import anyio
import pytest

from imp.engine.change import Change
from imp.engine.executor import RunResult, run_change
from tests.conftest import _git, _init_repo, write_agent_md


@pytest.fixture(autouse=True)
def _ensure_builtin_providers() -> None:
    # The provider registry is import-time global state; other tests clear it
    # via reset_providers_for_tests() teardown. Restore the built-ins so this
    # test is independent of xdist worker ordering.
    from imp.providers import register_builtin_providers
    register_builtin_providers()


def _seed_repo(root: Path) -> None:
    _init_repo(root)
    src = root / "imp" / "workflows" / "imp"
    (src / "agents").mkdir(parents=True)
    write_agent_md(
        src / "agents" / "driver.md",
        frontmatter=(
            "name: driver\n"
            "subagents: [coder]\n"
            "output:\n  status: {type: enum, values: [Success, Error]}"
        ),
    )
    write_agent_md(
        src / "agents" / "coder.md",
        frontmatter="name: coder\ntools: [bash]",
    )
    (src / "workflow.yaml").write_text(
        "name: t\ndescription: \"\"\n"
        "phases:\n  - name: p\n    steps:\n"
        "      - {name: s, file: agents/driver.md}\n"
    )
    (root / "imp" / "project.yaml").write_text("default-workflow: imp\n")
    (root / ".imp").mkdir(exist_ok=True)
    (root / ".imp" / "config.yaml").write_text(
        "providers: [scripted]\n"
        "tiers:\n"
        "  low: {scripted: x}\n"
        "  medium: {scripted: x}\n"
        "  high: {scripted: x}\n"
        "sandbox:\n  enabled: false\n"
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "imp setup")


def test_scripted_parent_dispatches_scripted_subagent(
    tmp_path: Path, monkeypatch,
) -> None:
    sentinel = "IMP_SUBAGENT_SENTINEL_xyz789"
    deliverable = "coder deliverable: sentinel committed"
    script = {
        "steps": {
            "driver": [
                {"text": "dispatching coder", "tools": [
                    {"name": "sub_agent", "input": {
                        "subagent_name": "coder",
                        "task_description": "write the sentinel file",
                        "tier": "medium",
                    }},
                ]},
                {"text": "done", "tools": [
                    {"name": "complete_step",
                     "input": {"outcome": "ok", "status": "Success"}},
                ]},
            ],
            # The coder is a subagent: no complete_step at depth 1; its final
            # bare turn-end text is the deliverable.
            "coder": [
                {"text": "working", "tools": [
                    {"name": "bash", "input": {"command": (
                        "mkdir -p src && "
                        f"printf '{sentinel}\\n' > src/sentinel.py && "
                        "git add -A && git commit -qm 'add sentinel'"
                    )}},
                ]},
                {"text": deliverable},
            ],
        },
    }
    script_path = tmp_path / "script.json"
    script_path.write_text(json.dumps(script))
    monkeypatch.setenv("IMP_SCRIPTED_SCRIPT", str(script_path))

    _seed_repo(tmp_path)
    ch = Change.create(tmp_path, title="x",
                       quest_id="2026-07-03-aaa111", base_branch="main")

    events: list[dict] = []
    result = anyio.run(functools.partial(run_change, ch, on_event=events.append))
    assert result is RunResult.COMPLETED

    # The coder (child) really ran: its commit is on the change worktree.
    worktree = tmp_path / ".imp" / "worktrees" / "2026-07-03-aaa111"
    committed = _git(worktree, "show", "HEAD:src/sentinel.py").stdout
    assert sentinel in committed

    # The driver received the coder's deliverable as the sub_agent tool_result.
    sub_results = [
        e for e in events
        if e["type"] == "tool_result"
        and deliverable in str(e["payload"].get("content", ""))
    ]
    assert sub_results, "sub_agent tool_result with the coder deliverable"
