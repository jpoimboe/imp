from __future__ import annotations

import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from imp.cli.prompt import OPENING_HINTS
from imp.paths import project_state_dir
from impty import TerminalSession
from tests._live import _await_change_dir
from tests.impty.e2e_support import (
    _MINIMAL_CONFIG,
    E2E_EXIT_POLL_TIMEOUT,
    E2E_POLL_TIMEOUT,
    _flock_free,
    _git,
    _poll,
    can_pty,
    clean_env,
    seed_minimal_repo,
)
from tests.impty.matchers import assert_screen

pytestmark = pytest.mark.skipif(not can_pty(), reason="pty not available")


def test_midrun_sigint_pauses_with_exit_130_and_a_resume_hint(tmp_path: Path) -> None:
    script = {"default": [{"text": "working", "tools": [
        # echo then a long sleep: the step is reliably mid-run when SIGINT lands.
        {"name": "bash", "input": {"command": "echo running; sleep 30"}},
        {"name": "complete_step", "input": {"status": "Success"}},
    ]}]}
    script_path = seed_minimal_repo(tmp_path, script=script)
    env = clean_env(IMP_SCRIPTED_SCRIPT=str(script_path), IMP_THEME="ansi")

    with TerminalSession(
        [sys.executable, "-m", "imp", "new", "demo", "--headless"],
        cwd=str(tmp_path), env=env, cols=80, rows=24,
    ) as s:
        # Poll until the scripted bash's "echo running" is visible, which
        # guarantees sleep 30 is running before SIGINT lands.
        deadline = time.monotonic() + E2E_POLL_TIMEOUT
        while "running" not in "\n".join(s.lines()):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            s.wait_idle(idle=0.1, hard=min(0.5, remaining))
        s.send_signal(signal.SIGINT)
        rc = s.run_to_completion(hard=E2E_EXIT_POLL_TIMEOUT)

        assert rc == 130                                   # interrupt → exit 130
        assert_screen(s, contains=("resume:", "imp resume"))  # resumable hint; rc==130 is pause


# First run signals worker_ready then sleeps; SIGINT lands mid-child. worker_ran guard skips resume.
_WORKER_CMD = (
    'D="$IMP_QUEST_DIR"; '
    'if [ -f "$D/worker_ran" ]; then exit 0; fi; '
    'touch "$D/worker_ran"; : > "$D/worker_ready"; sleep 30'
)


def _seed_subagent_repo(root: Path, *, script: dict) -> Path:
    """Seed a scripted-provider repo dispatching a `worker` subagent; returns script.json."""
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / ".gitignore").write_text(".imp/\n.imp-isolated/\n")
    (root / "README.md").write_text("seed\n")
    agents = root / "imp" / "workflows" / "imp" / "agents"
    agents.mkdir(parents=True)
    (agents / "driver.md").write_text(
        "---\nname: driver\nsubagents: [worker]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n"
        "---\ndriver body\n")
    (agents / "worker.md").write_text(
        "---\nname: worker\ntools: [bash]\n---\nworker body\n")
    (root / "imp" / "workflows" / "imp" / "workflow.yaml").write_text(
        "name: t\ndescription: \"\"\n"
        "phases:\n  - name: work\n    steps:\n"
        "      - name: driver\n        file: agents/driver.md\n")
    (root / "imp" / "config.yaml").write_text(
        "default-workflow: imp\ndefault-base: main\n")
    (project_state_dir(root) / "config.yaml").write_text(_MINIMAL_CONFIG)
    script_path = root / "script.json"
    script_path.write_text(json.dumps(script))
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "imp setup")
    return script_path


def test_sigint_during_subagent_hands_back_and_resumes(tmp_path: Path) -> None:
    script = {
        "steps": {
            "driver": [
                {"text": "dispatch worker", "tools": [
                    {"name": "sub_agent", "input": {
                        "subagent_name": "worker",
                        "task_description": "run the long job"}}]},
                {"text": "worker done", "tools": [
                    {"name": "complete_step", "input": {"status": "Success", "outcome": "done"}}]},
            ],
            # Subagent (depth 1): its final bare turn-end text is the deliverable.
            "worker": [
                {"text": "working", "tools": [
                    {"name": "bash", "input": {"command": _WORKER_CMD}}]},
                {"text": "worker deliverable"},
            ],
        },
    }
    script_path = _seed_subagent_repo(tmp_path, script=script)
    env = clean_env(IMP_SCRIPTED_SCRIPT=str(script_path), IMP_THEME="ansi")

    with TerminalSession(
        [sys.executable, "-m", "imp", "new", "demo", "--headless"],
        cwd=str(tmp_path), env=env, cols=80, rows=24,
    ) as s:
        quest_home = _await_change_dir(tmp_path, timeout=E2E_POLL_TIMEOUT)
        slug = quest_home.name
        # Poll the worker's readiness marker so SIGINT is guaranteed to land while
        # the CHILD subagent is running, not before it starts or after it returns.
        assert _poll(lambda: (quest_home / "worker_ready").exists(),
                     timeout=E2E_POLL_TIMEOUT), "worker subagent never started"
        s.send_signal(signal.SIGINT)
        rc = s.run_to_completion(hard=E2E_POLL_TIMEOUT)
        assert rc == 130                                   # interrupt -> pause

    # The parent's turn committed the [interrupted] handback to ITS transcript
    # before pausing (the durable-unit property) — not a dangling sub_agent
    # tool_use. Locate the parent step transcript (exclude the child's own).
    def _parent_tx() -> Path | None:
        cands = [p for p in quest_home.glob("steps/**/transcript.jsonl")
                 if "subagents" not in p.parts]
        return cands[0] if len(cands) == 1 else None

    def _has_interrupt() -> bool:
        tx = _parent_tx()
        return tx is not None and "[interrupted]" in tx.read_text()

    assert _poll(_has_interrupt, timeout=E2E_POLL_TIMEOUT), \
        "no [interrupted] handback in parent transcript"
    body = _parent_tx().read_text()
    assert "was interrupted before finishing" in body     # the handback header
    assert "imp events " in body                          # runnable recovery command
    assert "--agent sub:" in body                         # scoped to this one child
    assert "transcript.jsonl" not in body                 # no sandbox-masked path

    # The client exits 130 before its daemon tears down; resuming early hits the dying daemon.
    assert _poll(lambda: _flock_free(quest_home / "lock"), timeout=E2E_POLL_TIMEOUT), \
        "paused run's daemon never released the quest lock"

    # Resume continues the interrupt-paused step and runs to completion: the quest
    # recovers with the handback already in the parent's transcript (exit 0).
    with TerminalSession(
        [sys.executable, "-m", "imp", "resume", slug, "--headless"],
        cwd=str(tmp_path), env=env, cols=80, rows=24,
    ) as r:
        rc2 = r.run_to_completion(hard=E2E_POLL_TIMEOUT)
        assert rc2 == 0, "\n".join(r.lines())


_RUN_HEADER_RE = re.compile(r"^([a-z0-9]{6}) · new$", re.M)


def _wait_for_opening_prompt(s: TerminalSession) -> str:
    """Block until the bare opening prompt is drawn; return the quest id from its header."""
    deadline = time.monotonic() + E2E_POLL_TIMEOUT
    while True:
        rows = s.lines()
        header = _RUN_HEADER_RE.search("\n".join(rows))
        if header and any(r.startswith("> ") and OPENING_HINTS["new"] in r for r in rows):
            return header.group(1)
        remaining = deadline - time.monotonic()
        assert remaining > 0, "opening prompt never drawn:\n" + "\n".join(rows)
        s.wait_idle(idle=0.1, hard=min(0.5, remaining))


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGHUP], ids=["sigterm", "sighup"])
def test_leave_signal_at_opening_prompt_discards_the_draft(tmp_path: Path, sig: int) -> None:
    script = {"default": [{"text": "unused", "tools": [
        {"name": "complete_step", "input": {"status": "Success"}}]}]}
    script_path = seed_minimal_repo(tmp_path, script=script)
    env = clean_env(IMP_SCRIPTED_SCRIPT=str(script_path), IMP_THEME="ansi")

    with TerminalSession(
        [sys.executable, "-m", "imp", "new"],
        cwd=str(tmp_path), env=env, cols=80, rows=24,
    ) as s:
        qid = _wait_for_opening_prompt(s)
        s.send_signal(sig)
        rc = s.run_to_completion(hard=E2E_EXIT_POLL_TIMEOUT)
        assert rc == 130, "\n".join(s.lines())        # a clean leave, not death by signal
        assert_screen(s, contains=(f"{qid} · (unnamed) · discarded",),
                      excludes=("Traceback",))

    listed = subprocess.run(
        [sys.executable, "-m", "imp", "list", "--all", "--json"],
        cwd=str(tmp_path), env=env, capture_output=True, text=True, check=True)
    assert qid not in [q["id"] for q in json.loads(listed.stdout)]


def test_ctrl_c_hint_then_ctrl_d_at_opening_prompt_leaves_a_blank_above_the_spine(
    tmp_path: Path,
) -> None:
    script = {"default": [{"text": "unused", "tools": [
        {"name": "complete_step", "input": {"status": "Success"}}]}]}
    script_path = seed_minimal_repo(tmp_path, script=script)
    env = clean_env(IMP_SCRIPTED_SCRIPT=str(script_path), IMP_THEME="ansi")
    hint = "press Ctrl-D to pause & exit"

    with TerminalSession(
        [sys.executable, "-m", "imp", "new"],
        cwd=str(tmp_path), env=env, cols=80, rows=24,
    ) as s:
        qid = _wait_for_opening_prompt(s)
        s.send("\x03")
        assert _poll(lambda: hint in s.lines(), timeout=E2E_POLL_TIMEOUT), \
            "\n".join(s.lines())
        s.send("\x04")
        rc = s.run_to_completion(hard=E2E_EXIT_POLL_TIMEOUT)

        rows = s.lines()
        assert rc == 130, "\n".join(rows)
        at = rows.index(hint)
        assert rows[at + 1:at + 3] == ["", f"{qid} · (unnamed) · discarded"], "\n".join(rows)


def test_sigint_hint_at_opening_prompt_under_a_non_exec_wrapper(tmp_path: Path) -> None:
    script = {"default": [{"text": "unused", "tools": [
        {"name": "complete_step", "input": {"status": "Success"}}]}]}
    script_path = seed_minimal_repo(tmp_path, script=script)
    env = clean_env(IMP_SCRIPTED_SCRIPT=str(script_path), IMP_THEME="ansi")
    hint = "press Ctrl-D to pause & exit"

    # The trailing command stops sh exec-ing imp, so imp is not its process-group leader.
    with TerminalSession(
        ["sh", "-c", f"{shlex.quote(sys.executable)} -m imp new; exit $?"],
        cwd=str(tmp_path), env=env, cols=80, rows=24,
    ) as s:
        qid = _wait_for_opening_prompt(s)
        children = subprocess.run(
            ["pgrep", "-P", str(s.pid)],
            capture_output=True, text=True, check=True).stdout.split()
        assert len(children) == 1, children
        os.kill(int(children[0]), signal.SIGINT)

        def _hint_above_prompt() -> bool:
            rows = s.lines()
            return hint in rows and any(
                r.startswith("> ") for r in rows[rows.index(hint) + 1:])

        assert _poll(_hint_above_prompt, timeout=E2E_POLL_TIMEOUT), "\n".join(s.lines())
        s.send("\x04")
        rc = s.run_to_completion(hard=E2E_EXIT_POLL_TIMEOUT)

        rows = s.lines()
        assert rc == 130, "\n".join(rows)
        at = rows.index(hint)
        assert rows[at + 1:at + 3] == ["", f"{qid} · (unnamed) · discarded"], "\n".join(rows)
