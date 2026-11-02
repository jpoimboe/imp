from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import anyio
import pytest

from imp.cli.session_run import run_session_client
from imp.cli.signals import InterruptCoordinator
from imp.cli.terminal_watchdog import WatchedStream
from imp.engine.executor import run_quest
from imp.engine.quest import Quest
from imp.engine.state import State
from imp.paths import project_state_dir
from imp.remote.session.daemon import serve_quest_session
from tests.async_wait import HANG_BACKSTOP, wait_until
from tests.conftest import _git, _init_repo, write_agent_md

QID = "2026-09-22-agsave"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def _ensure_builtin_providers() -> None:
    from imp.providers import register_builtin_providers
    register_builtin_providers()


def _seed(root: Path) -> None:
    _init_repo(root)
    src = root / "imp" / "workflows" / "imp"
    write_agent_md(
        src / "agents" / "coder.md",
        frontmatter=(
            "name: coder\ntools: [save_and_exit, complete_step]\n"
            "output:\n  status: {type: enum, values: [Success, Error]}"
        ),
    )
    (src / "workflow.yaml").write_text(
        "name: t\ndescription: \"\"\n"
        "phases:\n  - name: p\n    steps:\n"
        "      - {name: s, file: agents/coder.md}\n"
    )
    (root / "imp" / "config.yaml").write_text("default-workflow: imp\n")
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


class _NullDriver:
    def on_event(self, _event: dict[str, Any]) -> None: ...
    def finish(self) -> None: ...
    def seed_pending_reply(self, _text: str) -> None: ...
    def install_startup_status(self) -> None: ...
    def show_stop_ack(self, _text: str) -> None: ...
    def clear_stop_ack(self) -> None: ...


async def _never(_prompt: str, _interrupt: bool = False) -> str:
    await anyio.sleep_forever()
    raise AssertionError("unreachable")


@pytest.mark.anyio
async def test_agent_save_ends_with_saved_as_draft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    script = {"default": [
        {"text": "setting this aside",
         "tools": [{"name": "save_and_exit", "input": {}}]},
        {"text": "LATER TURN",
         "tools": [{"name": "complete_step", "input": {"status": "Success"}}]},
    ]}
    script_path = tmp_path / "script.json"
    script_path.write_text(json.dumps(script))
    monkeypatch.setenv("IMP_SCRIPTED_SCRIPT", str(script_path))
    _seed(tmp_path)
    quest = Quest.create(tmp_path, title="x", quest_id=QID, base_branch="main")
    monkeypatch.setattr(
        "imp.remote.session.attach.spawn_session_daemon", lambda ch, **_kw: None)

    async def _run_after_attach(q: Quest, **kw: Any) -> Any:
        # The daemon awaits a first attach only at a gate; this run hits none
        # before the save, so without this the client can miss the socket.
        await wait_until(lambda: "cli_attached" in q.events_path.read_text())
        return await run_quest(q, **kw)

    rc: dict[str, int] = {}
    with anyio.fail_after(HANG_BACKSTOP):
        async with anyio.create_task_group() as tg:
            tg.start_soon(lambda: serve_quest_session(
                quest, run_fn=_run_after_attach, hibernate=False))

            async def _client() -> None:
                rc["rc"] = await run_session_client(
                    quest, driver=_NullDriver(), coord=InterruptCoordinator(),
                    solicit=_never, leave_mode="new",
                    stream=WatchedStream(io.StringIO()))

            tg.start_soon(_client)

    out = capsys.readouterr().out
    assert rc["rc"] == 130
    assert "lost the connection" not in out
    assert out.endswith(f"{QID} · x · saved as draft\nresume: imp resume {QID}\n\n")

    events = [json.loads(ln) for ln in quest.events_path.read_text().splitlines()]
    types = [e["type"] for e in events]
    assert types.count("agent_message") == 1  # the LATER TURN never ran
    call, res = types.index("tool_call"), types.index("tool_result")
    assert events[call]["payload"]["name"] == "save_and_exit"
    assert events[res]["payload"]["content"] == (
        "Saved. The session closes at the end of this turn.")
    assert call < res < types.index("conversation_saved")
    assert types[-1] == "conversation_saved"
    assert State.load(quest.state_dir).status == "paused"
