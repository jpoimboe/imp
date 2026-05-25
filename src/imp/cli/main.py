"""imp CLI entrypoint.

Plan 1: minimal commands (new, resume, log, status). No streaming, no signals,
no spinner. Plan 3 adds the polished UX layer.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
from pathlib import Path
from typing import Any

from imp.agents.runner import AgentRunner
from imp.agents.stub_runner import StubAgentRunner
from imp.engine.change import Change
from imp.engine.executor import RunResult, run
from imp.engine.state import LockHeld, ProjectLock, State


def _project_root() -> Path:
    """Plan 1 only: cwd is the project root. Plan 3 adds walk-up discovery."""
    return Path.cwd()


def _write_change_lock(change: Change) -> None:
    """Informational per-Change PID file (spec §5.5). Removed on exit. The
    ProjectLock already enforces single-runner; this just gives `imp status`
    something to display."""
    (change.change_dir / "lock").write_text(str(os.getpid()))


def _clear_change_lock(change: Change) -> None:
    with contextlib.suppress(FileNotFoundError):
        (change.change_dir / "lock").unlink()


def _agent_runner() -> AgentRunner:
    """Plan 1 ships only StubAgentRunner. Plan 2 wires the real adapter and
    selects via config; IMP_STUB_CANNED is honored for testing.
    """
    canned = os.environ.get("IMP_STUB_CANNED")
    if canned:
        return StubAgentRunner(canned_path=Path(canned))
    print(
        "imp: no agent runner available "
        "(Plan 1 supports stub only — set IMP_STUB_CANNED).",
        file=sys.stderr,
    )
    raise SystemExit(2)


def _cmd_new(args: argparse.Namespace) -> int:
    project = _project_root()
    lock = ProjectLock(project / ".imp" / "lock")
    try:
        with lock.acquire():
            change = Change.create(project, title=args.title)
            _write_change_lock(change)
            try:
                print(f"Created Change {change.id}: {change.title!r}")
                result = run(change, _agent_runner())
                print(f"Change {change.id}: {result.value}")
                return 0 if result is RunResult.COMPLETED else 1
            finally:
                _clear_change_lock(change)
    except LockHeld as e:
        print(
            f"imp: another Change is running (lock held by pid {e.held_by_pid})",
            file=sys.stderr,
        )
        return 2


def _replay_current_step(change: Change, state: State) -> None:
    if state.step is None:
        return
    events = list(change.event_log().events_for_current_step(state.step))
    if not events:
        return
    print("── replaying current step context ──")
    for e in events:
        t = e["type"]
        p = e["payload"]
        if t == "agent_message":
            print(f"[agent] {p.get('text', '')}")
        elif t == "tool_call":
            print(f"[tool ] {p.get('name', '')} {p.get('args', '')}")
        elif t == "tool_result":
            result = p.get("result", "")
            print(f"[tool ] → {result[:80] if isinstance(result, str) else result}")
        elif t == "user_input":
            print(f"[user ] {p.get('text', '')}")
        elif t == "agent_output":
            print(f"[out  ] {p}")
    print("── end replay ──")


def _cmd_resume(args: argparse.Namespace) -> int:
    project = _project_root()
    lock = ProjectLock(project / ".imp" / "lock")
    try:
        with lock.acquire():
            change = Change.load(project, args.change_id)
            state = State.load(change.state_dir)
            if state.status not in ("paused", "errored"):
                print(
                    f"imp: Change {change.id} status is {state.status!r}; "
                    "nothing to resume",
                    file=sys.stderr,
                )
                return 2
            _write_change_lock(change)
            try:
                print(f"Resuming Change {change.id}: {change.title!r}")
                print(f"Phase: {state.phase}, step: {state.step}")
                change.event_log().append("workflow_resumed", {})
                _replay_current_step(change, state)
                state.status = "running"
                state.save(change.state_dir)
                result = run(change, _agent_runner())
                print(f"Change {change.id}: {result.value}")
                return 0 if result is RunResult.COMPLETED else 1
            finally:
                _clear_change_lock(change)
    except LockHeld as e:
        print(
            f"imp: another Change is running (lock held by pid {e.held_by_pid})",
            file=sys.stderr,
        )
        return 2


def _cmd_log(args: argparse.Namespace) -> int:
    project = _project_root()
    change = Change.load(project, args.change_id)
    for e in change.event_log().read():
        ts = e["timestamp"][:19].replace("T", " ")
        if args.full:
            print(f"{ts}  {e['type']:<18}  {json.dumps(e['payload'])}")
        else:
            summary = _summarize_event(e)
            print(f"{ts}  {e['type']:<18}  {summary}")
    return 0


def _summarize_event(e: dict[str, Any]) -> str:
    t = e["type"]
    p = e["payload"]
    if t == "step_started":
        return f"{p.get('step', '')} [model_role={p.get('model_role', '')}]"
    if t == "step_completed":
        return f"{p.get('step', '')}  → {p.get('action', '')}"
    if t == "agent_output":
        return " ".join(f"{k}={v}" for k, v in p.items())
    if t == "phase_started":
        return str(p.get("phase", ""))
    if t == "phase_completed":
        return f"{p.get('phase', '')}  → {p.get('action', '')}"
    if t == "agent_message":
        text = str(p.get("text", ""))
        return text if len(text) < 60 else text[:57] + "..."
    return json.dumps(p)[:80]


def _cmd_status(args: argparse.Namespace) -> int:
    project = _project_root()
    changes_root = project / ".imp" / "changes"
    if not changes_root.is_dir():
        print("No Changes yet.")
        return 0
    rows = []
    for d in sorted(changes_root.iterdir(), key=lambda p: int(p.name)):
        if not d.is_dir() or not d.name.isdigit():
            continue
        meta = json.loads((d / "change.json").read_text())
        state = State.load(d / "state")
        rows.append(
            (meta["id"], state.status, state.phase or "-",
             state.step or "-", meta["title"])
        )
    print(f"{'ID':<4} {'STATUS':<10} {'PHASE':<12} {'STEP':<20} TITLE")
    for row in rows:
        print(f"{row[0]:<4} {row[1]:<10} {row[2]:<12} {row[3]:<20} {row[4]}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(prog="imp")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_new = sub.add_parser("new")
    p_new.add_argument("title", nargs="?")
    p_new.set_defaults(func=_cmd_new)

    p_resume = sub.add_parser("resume")
    p_resume.add_argument("change_id", type=int)
    p_resume.set_defaults(func=_cmd_resume)

    p_log = sub.add_parser("log")
    p_log.add_argument("change_id", type=int)
    p_log.add_argument("--full", action="store_true")
    p_log.set_defaults(func=_cmd_log)

    p_status = sub.add_parser("status")
    p_status.set_defaults(func=_cmd_status)

    ns = parser.parse_args(args)
    rc: int = ns.func(ns)
    return rc
