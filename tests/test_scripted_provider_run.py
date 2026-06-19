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


def _seed_scripted_repo(root: Path, script_path: Path) -> None:
    _init_repo(root)
    src = root / "imp" / "workflows" / "imp"
    (src / "agents").mkdir(parents=True)
    write_agent_md(
        src / "agents" / "coder.md",
        frontmatter=(
            "name: coder\ntools: [bash, complete_step]\n"
            "output:\n  status: {type: enum, values: [Success, Error]}"
        ),
    )
    (src / "workflow.yaml").write_text(
        "name: t\ndescription: \"\"\n"
        "phases:\n  - name: p\n    steps:\n"
        "      - {name: s, file: agents/coder.md}\n"
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


def test_scripted_tool_calls_produce_real_commit_and_sentinel(
    tmp_path: Path, monkeypatch,
) -> None:
    sentinel = "IMP_DOGFOOD_abc123"
    script = {
        "default": [
            {"text": "writing sentinel", "tools": [
                {"name": "bash", "input": {"command": (
                    "mkdir -p src && "
                    f"printf '{sentinel}\\n' > src/sentinel.py && "
                    "git add -A && git commit -qm 'add sentinel'"
                )}},
                {"name": "complete_step", "input": {"outcome": "ok", "status": "Success"}},
            ]},
        ],
    }
    script_path = tmp_path / "script.json"
    script_path.write_text(json.dumps(script))
    monkeypatch.setenv("IMP_SCRIPTED_SCRIPT", str(script_path))

    _seed_scripted_repo(tmp_path, script_path)
    ch = Change.create(tmp_path, title="x",
                       change_id="2026-06-12-aaa111", base_branch="main")

    events: list[dict] = []
    result = anyio.run(functools.partial(run_change, ch, on_event=events.append))
    assert result is RunResult.COMPLETED

    worktree = tmp_path / ".imp" / "worktrees" / "2026-06-12-aaa111"
    committed = _git(worktree, "show", "HEAD:src/sentinel.py").stdout
    assert sentinel in committed

    bash_calls = [e for e in events
                  if e["type"] == "tool_call"
                  and e["payload"].get("name") == "bash"]
    assert bash_calls

