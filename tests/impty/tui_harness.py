"""Runnable harness: drive the real TuiApp over a pty for the impty pyte tests.

Usage: python tests/impty/tui_harness.py <scenario> <result_path>
Scenarios:
  view   — feed a tall narrative, run forever (the test sends keys, then kills).
  exit   — feed a tall narrative, run, exit after a short timer, then print the
           collapsed transcript to stdout (normal scrollback).
  prompt — feed a small narrative, run, solicit once, write the reply line to
           <result_path>, then exit.
  promptscroll — like prompt but with a TALL narrative, so the test can scroll /
           toggle verbosity while the solicit is active (Finding 4 regression).

No agent, no provider: events are synthesized so the run is deterministic and
offline. PYTHONPATH=src is pinned by tests/conftest.py for the child.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
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


def _git_q(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args],
                   check=True, capture_output=True, text=True)


def _seed_run_repo(root: Path, script_path: Path) -> None:
    _git_q(root, "init", "-q", "-b", "main")
    _git_q(root, "config", "user.email", "t@t")
    _git_q(root, "config", "user.name", "t")
    (root / ".gitignore").write_text(".imp/\n")
    (root / "README.md").write_text("seed\n")
    src = root / "imp" / "workflows" / "imp" / "agents"
    src.mkdir(parents=True)
    (src / "coder.md").write_text(
        "---\nname: coder\ntools: [bash, complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n"
        "---\nprompt body\n")
    (root / "imp" / "workflows" / "imp" / "workflow.yaml").write_text(
        "name: t\ndescription: \"\"\n"
        "phases:\n  - name: p\n    steps:\n"
        "      - {name: s, file: agents/coder.md}\n")
    (root / "imp" / "project.yaml").write_text("default-workflow: imp\n")
    (root / ".imp").mkdir(exist_ok=True)
    (root / ".imp" / "config.yaml").write_text(
        "providers: [scripted]\n"
        "tiers:\n"
        "  low: {scripted: x}\n"
        "  medium: {scripted: x}\n"
        "  high: {scripted: x}\n"
        "sandbox:\n  enabled: false\n")
    tools = [{"name": "bash", "input": {"command": f"echo {i}"}} for i in range(40)]
    tools.append({"name": "complete_step", "input": {"status": "Success"}})
    script_path.write_text(json.dumps({"default": [{"text": "working", "tools": tools}]}))
    _git_q(root, "add", "-A")
    _git_q(root, "commit", "-qm", "imp setup")


async def _run_real_engine(app: TuiApp) -> None:
    from imp.engine.change import Change
    from imp.engine.executor import run_change

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        script_path = root / "script.json"
        _seed_run_repo(root, script_path)
        os.environ["IMP_SCRIPTED_SCRIPT"] = str(script_path)
        change = Change.create(root, title="x",
                               change_id="2026-06-12-run0", base_branch="main")

        async def _drive() -> None:
            await run_change(change, on_event=app.on_event, non_interactive=True)

        _task = asyncio.create_task(_drive())
        await app.run_async()
        await asyncio.gather(_task, return_exceptions=True)


async def _main(scenario: str, result_path: str) -> None:
    if scenario == "run":
        app = TuiApp(coord=InterruptCoordinator(), color=True, width=80)
        await _run_real_engine(app)
        return

    if scenario == "theme":
        from imp.cli import palette as pal
        pal.set_theme(os.environ.get("IMP_THEME", "dark"))
        app = TuiApp(coord=InterruptCoordinator(), color=True, width=80)
        for e in _events(3):          # short narrative: header stays on-screen
            app.on_event(e)
        app._build_app()
        await app.run_async()
        return

    app = TuiApp(coord=InterruptCoordinator(), color=True, width=80)
    n_tools = 4 if scenario == "prompt" else 40  # promptscroll/view/exit need scrollback
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

    if scenario in ("prompt", "promptscroll"):
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
