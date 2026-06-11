"""Runnable harness: drive the real TuiApp over a pty for the impty pyte tests.

Usage: python tests/impty/tui_harness.py <scenario> <result_path>
Scenarios:
  view   — feed a tall narrative, run forever (the test sends keys, then kills).
  exit   — feed a tall narrative, run, exit after a short timer, then print the
           collapsed transcript to stdout (normal scrollback).
  prompt — feed a small narrative, run, solicit once, write the reply line to
           <result_path>, then exit.

No agent, no provider: events are synthesized so the run is deterministic and
offline. PYTHONPATH=src is pinned by tests/conftest.py for the child.
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

from imp.cli.signals import InterruptCoordinator
from imp.cli.tui_app import TuiApp


def _events(n_tools: int) -> list[dict]:
    evs: list[dict] = [
        {"id": "1", "agent_id": None, "type": "change_created",
         "payload": {"change_id": 1, "title": "x"}},
        {"id": "2", "agent_id": None, "type": "step_started",
         "payload": {"step": "spec", "kind": "agent", "phase": "design",
                     "model": "m", "provider": "anthropic"}},
    ]
    for i in range(n_tools):
        evs.append({"id": f"c{i}", "agent_id": "step:design/spec", "type": "tool_call",
                    "payload": {"id": f"id{i}", "name": "bash",
                                "input": {"command": f"echo {i}"}}})
        evs.append({"id": f"r{i}", "agent_id": "step:design/spec", "type": "tool_result",
                    "payload": {"id": f"id{i}", "output": "ok"}})
    evs.append({"id": "3", "agent_id": None, "type": "step_completed",
                "payload": {"step": "spec", "kind": "agent",
                            "action": {"verb": "continue"}, "var_mutations": {}}})
    return evs


class _Change:
    def __init__(self, workflow_dir: Path) -> None:
        self.workflow_dir = workflow_dir


async def _main(scenario: str, result_path: str) -> None:
    app = TuiApp(coord=InterruptCoordinator(), color=True, width=80)
    n_tools = 4 if scenario == "prompt" else 40
    for e in _events(n_tools):
        app.on_event(e)

    app._build_app()  # harness drives the app directly

    if scenario == "view":
        await app.run_async()
        return

    if scenario == "exit":
        # Gate exit on a keystroke the test sends, not a timer: a fixed timer
        # races subprocess-startup latency against the idle-settle the test uses
        # to observe the alt-screen, so it can exit before the test ever sees it.
        @app._app.key_bindings.add("q")
        def _(event: object) -> None:
            app.exit()
        await app.run_async()
        app.print_collapsed_transcript(sys.stdout)
        sys.stdout.flush()
        return

    if scenario == "prompt":
        with tempfile.TemporaryDirectory() as tmpdir:
            wf = Path(tmpdir) / "wf"
            (wf / "skills").mkdir(parents=True)
            change = _Change(wf)

            async def _ask() -> None:
                line = await app.solicit(change)
                Path(result_path).write_text(line)
                app.exit()
            _task = asyncio.create_task(_ask())
            await app.run_async()
            await asyncio.gather(_task, return_exceptions=True)
        return


if __name__ == "__main__":
    asyncio.run(_main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else ""))
