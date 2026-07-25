"""Mid-step Ctrl+C resume against real provider APIs.

Verifies §6.15 mechanics end-to-end:
  1. Spawn `imp new` as a subprocess.
  2. Wait for the writer's bash subprocess to start (poll for the
     events.jsonl `tool_call` event named "bash").
  3. Send SIGINT to the imp subprocess.
  4. Confirm the imp subprocess exits cleanly with code 130.
  5. Confirm state.json is "paused" with the writer step still set.
  6. Spawn `imp resume <slug>`; confirm completion.

Each test skips independently based on its own provider key/CLI availability.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import NoReturn

import pytest

from tests._live import (
    _await_change_dir,
    anthropic_key,
    openai_key,
    sandbox_sleep_precondition_failed,
    wait_for_archived_status,
    wait_for_bash_tool_call,
)
from tests.conftest import _WORKTREE_SRC, _seed_project


def _wait_for_paused(state_path: Path, timeout: float) -> dict[str, object]:
    """Poll state.json until status=='paused', returning the parsed state.

    The interrupt's pause-write (state.save("paused") + the workflow_paused
    event) happens ASYNCHRONOUSLY in the session daemon, AFTER the client
    process exits 130. The client can return ahead of that write: a mid-step
    interrupt first surfaces a bare-turn-end `awaiting_user_input` gate, and the
    client's solicit hits EOF on the test's piped stdin and tears down before the
    daemon settles into pause. Reading state.json once immediately therefore
    races the write (visible with claude_code, whose interrupt path — in-flight
    bash → rejected tool use → query error → turn end — is slow enough to lose
    the race; in-process anthropic/openai usually win it). Poll for the durable
    paused state instead of assuming a synchronous write."""
    deadline = time.time() + timeout
    last: dict[str, object] = {}
    while time.time() < deadline:
        try:
            last = json.loads(state_path.read_text())
        except (OSError, json.JSONDecodeError):
            time.sleep(0.1)
            continue
        if last.get("status") == "paused":
            return last
        time.sleep(0.1)
    return last


def _skip_or_fail_unpaused(
    events: Path, state: dict[str, object], proc: subprocess.Popen
) -> NoReturn:
    """Skip on the overlay-mount precondition failure, else fail with diagnostics."""
    reason = sandbox_sleep_precondition_failed(events)
    if reason is not None:
        pytest.skip(reason)
    out = proc.stdout.read().decode(errors="replace") if proc.stdout else ""
    err = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
    tail = (
        "\n".join(events.read_text().splitlines()[-40:])
        if events.exists() else "<no events.jsonl>"
    )
    pytest.fail(
        f"expected state 'paused', got {state.get('status')!r}\n"
        f"state.json: {state}\n"
        f"events.jsonl tail:\n{tail}\n"
        f"subprocess stdout:\n{out}\n"
        f"subprocess stderr:\n{err}"
    )


@pytest.mark.live("anthropic")
# 600s > this test's ~360-480s internal subprocess-timeout budget, so conftest's
# 240s live-test backstop can't fire mid-subprocess before imp's own internal
# timeouts do (which yield real diagnostics instead of a killed worker).
@pytest.mark.timeout(600)
def test_ctrl_c_during_bash_pauses_then_resume_completes(tmp_path: Path) -> None:
    _seed_project(tmp_path)
    env = os.environ.copy()
    key = anthropic_key()
    assert key is not None
    env["ANTHROPIC_API_KEY"] = key
    env.pop("CLAUDECODE", None)  # avoid stub-api-key clamp from conftest
    _pypath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = _WORKTREE_SRC + (os.pathsep + _pypath if _pypath else "")

    # cwd=tmp_path is what makes imp find the project root — find_project_root
    # walks up from cwd looking for the imp/project.yaml marker. No
    # IMP_PROJECT_ROOT env var needed
    # (the engine sets that internally for script gates, but it's not read at
    # CLI startup).
    proc = subprocess.Popen(
        [sys.executable, "-m", "imp", "new", "mid-step-resume"],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        # Foreground process group so SIGINT reaches the imp process directly.
        start_new_session=True,
    )
    try:
        # imp new starts an un-materialized draft: no worktree, state+events live
        # at the permanent control-root home .imp/quests/<id>/.
        quest_dir = _await_change_dir(tmp_path, timeout=30.0)
        slug = quest_dir.name
        events = quest_dir / "events.jsonl"
        state_path = quest_dir / "state" / "state.json"

        if not wait_for_bash_tool_call(events, timeout=60.0):
            stdout, stderr = proc.communicate(timeout=10)
            pytest.fail(
                f"never saw bash tool_call event; stdout={stdout!r} stderr={stderr!r}"
            )
        time.sleep(1.0)
        os.killpg(proc.pid, signal.SIGINT)
        # Generous deadlines: under `-n` concurrency the post-SIGINT exit and the
        # daemon's async pause-write both stretch; these only bite on a real hang.
        rc = proc.wait(timeout=60)
        assert rc == 130, f"expected exit 130, got {rc}"

        state = _wait_for_paused(state_path, timeout=30.0)
        if state.get("status") != "paused":
            _skip_or_fail_unpaused(events, state, proc)
        assert state["step"] == "w"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()

    # Resume by slug; the minimal workflow never goes live, so it completes as a
    # done draft — the home is archived and reaped, so read the terminal status
    # from the archive.
    resume = subprocess.run(
        [sys.executable, "-m", "imp", "resume", slug],
        cwd=tmp_path, env=env,
        capture_output=True, text=True, timeout=180,
    )
    assert resume.returncode == 0, (
        f"resume failed rc={resume.returncode} stdout={resume.stdout} stderr={resume.stderr}"
    )

    final_status = wait_for_archived_status(tmp_path, slug, "done", 30.0)
    assert final_status == "done", final_status


@pytest.mark.live("openai")
# 600s > this test's ~360-480s internal subprocess-timeout budget, so conftest's
# 240s live-test backstop can't fire mid-subprocess before imp's own internal
# timeouts do (which yield real diagnostics instead of a killed worker).
@pytest.mark.timeout(600)
def test_ctrl_c_during_bash_pauses_then_resume_completes_openai(
    tmp_path: Path,
) -> None:
    _seed_project(tmp_path)
    # Override the anthropic project.yaml _seed_project committed; re-commit so
    # the subprocess `imp new` worktree (project layer read from the committed
    # tree) carries the openai pin.
    # gpt-5.4-mini, not gpt-4o-mini: the model isn't under test here (the
    # interrupt/resume machinery is), but it MUST reliably run the scripted
    # bash(`sleep 30`) so the SIGINT lands mid-bash. gpt-4o-mini intermittently
    # skipped the bash call and completed the step immediately, so the test saw no
    # bash tool_call and failed. gpt-5.4-mini (the project's openai `low` pin)
    # follows the bash-then-complete sequence reliably.
    (tmp_path / "imp" / "project.yaml").write_text(textwrap.dedent("""\
        default-workflow: imp
        default-base: main
        providers: [openai]
        tiers:
          low: {openai: gpt-5.4-mini}
          medium: {openai: gpt-5.4-mini}
          high: {openai: gpt-5.4-mini}
    """))
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-qm", "openai config"],
        cwd=tmp_path, check=True,
    )
    env = os.environ.copy()
    key = openai_key()
    assert key is not None
    env["OPENAI_API_KEY"] = key
    env.pop("CLAUDECODE", None)
    _pypath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = _WORKTREE_SRC + (os.pathsep + _pypath if _pypath else "")

    proc = subprocess.Popen(
        [sys.executable, "-m", "imp", "new", "mid-step-resume-oa"],
        cwd=tmp_path, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        # imp new starts an un-materialized draft: no worktree, state+events live
        # at the permanent control-root home .imp/quests/<id>/.
        quest_dir = _await_change_dir(tmp_path, timeout=30.0)
        slug = quest_dir.name
        events = quest_dir / "events.jsonl"
        state_path = quest_dir / "state" / "state.json"

        if not wait_for_bash_tool_call(events, timeout=60.0):
            stdout, stderr = proc.communicate(timeout=10)
            pytest.fail(f"no bash; stdout={stdout!r} stderr={stderr!r}")
        time.sleep(1.0)
        os.killpg(proc.pid, signal.SIGINT)
        rc = proc.wait(timeout=60)
        assert rc == 130
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()

    state = _wait_for_paused(state_path, timeout=30.0)
    if state.get("status") != "paused":
        _skip_or_fail_unpaused(events, state, proc)

    resume = subprocess.run(
        [sys.executable, "-m", "imp", "resume", slug],
        cwd=tmp_path, env=env,
        capture_output=True, text=True, timeout=180,
    )
    assert resume.returncode == 0, (
        f"resume failed rc={resume.returncode}; stderr={resume.stderr}"
    )


@pytest.mark.live("claude_code")
# Serialize the 3 crash-prone resume tests onto one worker (under --dist
# loadgroup) to cap the peak concurrent claude-CLI footprint they contribute.
@pytest.mark.xdist_group("live_claude_resume")
# 880s > this test's 752s internal subprocess-timeout budget
# (30 + 120 + 2 + 60 + 30 + 480 + 30), so conftest's 240s live-test backstop can't
# fire mid-subprocess before imp's own internal timeouts do (which yield real
# diagnostics instead of a killed worker).
@pytest.mark.timeout(880)
def test_ctrl_c_during_bash_pauses_then_resume_completes_claude_code(
    tmp_path: Path,
) -> None:
    _seed_project(tmp_path)
    # Override the anthropic project.yaml _seed_project committed; re-commit so
    # the subprocess `imp new` worktree (project layer read from the committed
    # tree) carries the claude_code pin.
    (tmp_path / "imp" / "project.yaml").write_text(textwrap.dedent("""\
        default-workflow: imp
        default-base: main
        providers: [claude_code]
        tiers:
          low: {claude_code: claude-haiku-4-5-20251001}
          medium: {claude_code: claude-sonnet-4-5}
          high: {claude_code: claude-opus-4-5}
    """))
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-qm", "claude_code config"],
        cwd=tmp_path, check=True,
    )
    env = os.environ.copy()
    env.pop("CLAUDECODE", None)
    _pypath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = _WORKTREE_SRC + (os.pathsep + _pypath if _pypath else "")

    proc = subprocess.Popen(
        [sys.executable, "-m", "imp", "new", "mid-step-resume-cc"],
        cwd=tmp_path, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        # imp new starts an un-materialized draft: no worktree, state+events live
        # at the permanent control-root home .imp/quests/<id>/.
        quest_dir = _await_change_dir(tmp_path, timeout=30.0)
        slug = quest_dir.name
        events = quest_dir / "events.jsonl"
        state_path = quest_dir / "state" / "state.json"

        if not wait_for_bash_tool_call(events, timeout=120.0):
            stdout, stderr = proc.communicate(timeout=10)
            pytest.fail(f"no bash; stdout={stdout!r} stderr={stderr!r}")
        time.sleep(2.0)  # CC subprocess startup is slower; extra grace
        os.killpg(proc.pid, signal.SIGINT)
        rc = proc.wait(timeout=60)
        assert rc == 130
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()

    state = _wait_for_paused(state_path, timeout=30.0)
    if state.get("status") != "paused":
        _skip_or_fail_unpaused(events, state, proc)

    resume = subprocess.run(
        [sys.executable, "-m", "imp", "resume", slug],
        cwd=tmp_path, env=env,
        capture_output=True, text=True, timeout=480,
    )
    assert resume.returncode == 0, (
        f"resume failed rc={resume.returncode}; stderr={resume.stderr}"
    )

    final_status = wait_for_archived_status(tmp_path, slug, "done", 30.0)
    assert final_status == "done", final_status
