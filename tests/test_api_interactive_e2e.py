"""AC 4: create -> stream -> answer a live gate -> completion, public API only."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from imp.api import attach, create_quest
from imp.paths import project_state_dir
from tests.conftest import _git, _init_repo, write_agent_md


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
async def _ignore_anyio_teardown_setresult_race() -> AsyncIterator[None]:
    """Swallow anyio 4.8's benign socket-teardown InvalidStateError.

    Exiting `attach()` cancels `_pump` while it is parked in `receive_events()`, and
    anyio registers the bound ``future.set_result`` as the raw fd-readable reader for
    stream reads (_backends/_asyncio.py:1550), removing it only in the future's
    done-callback. When the socket tears down in the SAME loop turn the fd goes
    readable, the already-queued ``set_result`` fires on the just-resolved future ->
    InvalidStateError, which asyncio routes to the loop exception handler and
    pytest-anyio promotes to a test failure -- measured at 3/20 runs with this
    fixture removed. Delegate every other loop exception to the previous handler
    untouched."""
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()

    def handler(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        exc = context.get("exception")
        callback = getattr(context.get("handle"), "_callback", None)
        future = getattr(callback, "__self__", None)
        if (
            isinstance(exc, asyncio.InvalidStateError)
            and getattr(callback, "__name__", "") == "set_result"
            and isinstance(future, asyncio.Future)
            and future.done()
        ):
            # the anyio read set_result-on-done-future race
            return  # the anyio accept/read set_result-on-done-future race
        if previous is not None:
            previous(loop, context)
        else:
            loop.default_exception_handler(context)

    loop.set_exception_handler(handler)
    try:
        yield
    finally:
        loop.set_exception_handler(previous)


def _seed_scripted_interactive_repo(root: Path) -> None:
    """test_api_background's seed with an `interactive: true` step."""
    # That flag is what makes the agent's first bare turn-end solicit rather than be
    # absorbed by the one-shot completion nudge.
    _init_repo(root)
    src = root / "imp" / "workflows" / "imp"
    (src / "agents").mkdir(parents=True)
    write_agent_md(
        src / "agents" / "interviewer.md",
        frontmatter=(
            "name: interviewer\ntools: [complete_step]\n"
            "output:\n  status: {type: enum, values: [Success, Error]}"
        ),
    )
    (src / "workflow.yaml").write_text(
        'name: t\ndescription: ""\n'
        "phases:\n  - name: p\n    steps:\n"
        "      - {name: s, file: agents/interviewer.md, interactive: true}\n"
    )
    (root / "imp" / "config.yaml").write_text(
        "default-workflow: imp\ndefault-base: main\n")
    (project_state_dir(root) / "config.yaml").write_text(
        "providers: [scripted]\n"
        "tiers:\n"
        "  low: {scripted: x}\n"
        "  medium: {scripted: x}\n"
        "  high: {scripted: x}\n"
        "sandbox:\n  enabled: false\n"
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "imp setup")


@pytest.mark.timeout(300)   # real daemon subprocess + git ops under -n auto
@pytest.mark.anyio
async def test_create_stream_answer_and_complete_through_the_public_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = tmp_path / "script.json"
    script.write_text(json.dumps({"default": [
        {"text": "which jurisdiction?"},                      # bare turn end -> solicits
        {"text": "done", "tools": [
            {"name": "complete_step",
             "input": {"outcome": "ok", "status": "Success"}}]},
    ]}))
    monkeypatch.setenv("IMP_SCRIPTED_SCRIPT", str(script))
    monkeypatch.setenv("IMP_SESSION_CONNECT_TIMEOUT", "240")  # daemon startup under load
    _seed_scripted_interactive_repo(tmp_path)

    handle = create_quest(tmp_path, "interview me")

    seen: list[str] = []
    answered = False
    # No polling, no `imp list`: the handle's id is attachable the instant it exists.
    async with attach(tmp_path, handle) as session:
        async for event in session.events():
            seen.append(event.type)
            if (event.type == "awaiting_user_input"
                    and not event.raw.get("_replay") and not answered):
                answered = True
                await session.send_input("us")
            if event.type == "workflow_completed":
                break

    assert answered, f"no live gate ever opened; saw {seen}"
    assert "user_input_resolved" in seen, f"the reply was never accepted; saw {seen}"
    assert "workflow_completed" in seen, f"workflow never reached completion; saw {seen}"
    assert seen.index("user_input_resolved") < seen.index("workflow_completed")
