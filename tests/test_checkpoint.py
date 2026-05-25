"""SqliteSaver per Change: checkpoint.db at state/checkpoint.db."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from imp.agents.runner import AgentInvocation
from imp.engine.change import Change
from imp.engine.executor import run

FIXTURES = Path(__file__).parent / "fixtures"


def test_checkpointer_passed_to_runner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    seen: list[AgentInvocation] = []

    class _Capture:
        def run(self, inv: AgentInvocation) -> Iterator[dict[str, Any]]:
            seen.append(inv)
            yield {"type": "agent_output", "payload": {"status": "Success"}}

    shutil.copytree(
        FIXTURES / "workflows" / "integration",
        tmp_path / ".imp" / "workflows" / "default",
    )
    (tmp_path / ".imp" / "config.yaml").write_text(
        "roles:\n  default: {prefer: [{provider: anthropic, model: x}]}\n"
    )
    monkeypatch.chdir(tmp_path)
    change = Change.create(tmp_path, title="t")
    run(change, _Capture())

    assert seen
    assert seen[0].checkpointer is not None
