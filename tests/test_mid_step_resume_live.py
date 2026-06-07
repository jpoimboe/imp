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
import shutil
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from tests._live import _await_worktree, opted_in
from tests.conftest import _init_repo

# Ensure subprocesses use this worktree's imp, not the editable install
# pointing to master (which has different code).
_WORKTREE_SRC = str(Path(__file__).parent.parent / "src")


def _anthropic_key() -> str | None:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key
    key_file = Path.home() / ".anthropic-key"
    if key_file.exists():
        return key_file.read_text().strip()
    return None


def _seed_project(tmp_path: Path) -> None:
    """Produce the committed imp/ layout the worktree model requires.

    imp new needs >=1 commit with HEAD on a branch (worktree.check_preconditions)
    and reads the workflow + config from the control root's WORKING TREE
    (Change.create copies imp/workflows/default; load_config reads imp/config.yaml).
    Committing them is the clean choice. _init_repo already commits a README +
    .gitignore with `.imp/`, so .imp/ is durably ignored and `imp new` won't add
    an extra gitignore commit.
    """
    _init_repo(tmp_path)
    wf = tmp_path / "imp" / "workflows" / "default"
    (wf / "agents").mkdir(parents=True)
    (wf / "agents" / "writer.md").write_text(textwrap.dedent("""\
        ---
        name: writer
        description: A test agent that calls bash then complete_step.
        tools: [complete_step, bash]
        output:
          status: {type: enum, values: [Success, Error]}
        ---
        Run this shell command via bash: `sleep 30`. After bash returns
        (even if it was interrupted), call complete_step with status=Success.
        Do not call any other tool.
    """))
    (wf / "workflow.yaml").write_text(textwrap.dedent("""\
        name: t
        description: t
        defaults: {max_retries: 2, max_loops: 6, on_max_retries: {pause: true}}
        phases:
          - name: only
            steps:
              - {name: w, file: agents/writer.md, tier: medium}
    """))
    (tmp_path / "imp" / "config.yaml").write_text(textwrap.dedent("""\
        providers: [anthropic]
        tiers:
          low: {anthropic: claude-haiku-4-5-20251001}
          medium: {anthropic: claude-sonnet-4-6}
          high: {anthropic: claude-opus-4-7}
    """))
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-qm", "imp setup"],
        cwd=tmp_path, check=True,
    )


def _wait_for_bash_tool_call(events_path: Path, timeout: float) -> bool:
    """Re-scan events.jsonl from the start until a tool_call(name=bash) event
    appears. Returns True on found, False on timeout.

    Re-scanning each tick (instead of tracking an offset) sidesteps the
    partial-line read race: EventLog.append issues two `write()` calls
    (`f.write(json); f.write("\\n"); f.flush(); fsync()`), so a reader can
    in theory catch the file between them. events.jsonl during a single
    step is tens of lines at most — cheap to re-read."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not events_path.exists():
            time.sleep(0.2)
            continue
        try:
            text = events_path.read_text()
        except OSError:
            time.sleep(0.2)
            continue
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue  # partial write; next tick picks it up
            if (rec.get("type") == "tool_call"
                    and rec.get("payload", {}).get("name") == "bash"):
                return True
        time.sleep(0.2)
    return False


@pytest.mark.skipif(
    not opted_in("anthropic"),
    reason="anthropic not in IMP_LIVE_PROVIDERS",
)
def test_ctrl_c_during_bash_pauses_then_resume_completes(tmp_path: Path) -> None:
    assert _anthropic_key() is not None, (
        "anthropic opted in via IMP_LIVE_PROVIDERS but no "
        "ANTHROPIC_API_KEY env var or ~/.anthropic-key file found"
    )
    _seed_project(tmp_path)
    env = os.environ.copy()
    key = _anthropic_key()
    assert key is not None
    env["ANTHROPIC_API_KEY"] = key
    env.pop("CLAUDECODE", None)  # avoid stub-api-key clamp from conftest
    _pypath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = _WORKTREE_SRC + (os.pathsep + _pypath if _pypath else "")

    # cwd=tmp_path is what makes imp find the project root — find_project_root
    # walks up from cwd looking for .imp/. No IMP_PROJECT_ROOT env var needed
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
        # Discover the worktree the engine created (its .name is the slug).
        wt = _await_worktree(tmp_path, timeout=30.0)
        slug = wt.name
        events = wt / ".imp" / "change" / "events.jsonl"
        state_path = wt / ".imp" / "change" / "state" / "state.json"

        if not _wait_for_bash_tool_call(events, timeout=60.0):
            stdout, stderr = proc.communicate(timeout=10)
            pytest.fail(
                f"never saw bash tool_call event; stdout={stdout!r} stderr={stderr!r}"
            )
        time.sleep(1.0)
        os.killpg(proc.pid, signal.SIGINT)
        rc = proc.wait(timeout=15)
        assert rc == 130, f"expected exit 130, got {rc}"

        state = json.loads(state_path.read_text())
        assert state["status"] == "paused"
        assert state["step"] == "w"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()

    # Resume by slug; the minimal workflow has no merge gate, so the worktree
    # persists and state.json reads "completed" from it (no journal needed).
    resume = subprocess.run(
        [sys.executable, "-m", "imp", "resume", slug],
        cwd=tmp_path, env=env,
        capture_output=True, text=True, timeout=180,
    )
    assert resume.returncode == 0, (
        f"resume failed rc={resume.returncode} stdout={resume.stdout} stderr={resume.stderr}"
    )

    final_state = json.loads(state_path.read_text())
    assert final_state["status"] == "completed"


def _openai_key() -> str | None:
    key = os.environ.get("OPENAI_API_KEY")
    if key:
        return key
    key_file = Path.home() / ".openai-key"
    if key_file.exists():
        return key_file.read_text().strip()
    return None


@pytest.mark.skipif(
    not opted_in("openai"),
    reason="openai not in IMP_LIVE_PROVIDERS",
)
def test_ctrl_c_during_bash_pauses_then_resume_completes_openai(
    tmp_path: Path,
) -> None:
    assert _openai_key() is not None, (
        "openai opted in via IMP_LIVE_PROVIDERS but no "
        "OPENAI_API_KEY env var or ~/.openai-key file found"
    )
    _seed_project(tmp_path)
    (tmp_path / "imp" / "config.yaml").write_text(textwrap.dedent("""\
        providers: [openai]
        tiers:
          low: {openai: gpt-4o-mini}
          medium: {openai: gpt-4o-mini}
          high: {openai: gpt-4o-mini}
    """))
    env = os.environ.copy()
    key = _openai_key()
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
        wt = _await_worktree(tmp_path, timeout=30.0)
        slug = wt.name
        events = wt / ".imp" / "change" / "events.jsonl"
        state_path = wt / ".imp" / "change" / "state" / "state.json"

        if not _wait_for_bash_tool_call(events, timeout=60.0):
            stdout, stderr = proc.communicate(timeout=10)
            pytest.fail(f"no bash; stdout={stdout!r} stderr={stderr!r}")
        time.sleep(1.0)
        os.killpg(proc.pid, signal.SIGINT)
        rc = proc.wait(timeout=15)
        assert rc == 130
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()

    state = json.loads(state_path.read_text())
    assert state["status"] == "paused"

    resume = subprocess.run(
        [sys.executable, "-m", "imp", "resume", slug],
        cwd=tmp_path, env=env,
        capture_output=True, text=True, timeout=180,
    )
    assert resume.returncode == 0, (
        f"resume failed rc={resume.returncode}; stderr={resume.stderr}"
    )


@pytest.mark.skipif(
    not opted_in("claude_code"),
    reason="claude_code not in IMP_LIVE_PROVIDERS",
)
def test_ctrl_c_during_bash_pauses_then_resume_completes_claude_code(
    tmp_path: Path,
) -> None:
    assert shutil.which("claude") is not None, (
        "claude_code opted in via IMP_LIVE_PROVIDERS but `claude` CLI "
        "not found on PATH"
    )
    _seed_project(tmp_path)
    (tmp_path / "imp" / "config.yaml").write_text(textwrap.dedent("""\
        providers: [claude_code]
        tiers:
          low: {claude_code: claude-haiku-4-5-20251001}
          medium: {claude_code: claude-sonnet-4-5}
          high: {claude_code: claude-opus-4-5}
    """))
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
        wt = _await_worktree(tmp_path, timeout=30.0)
        slug = wt.name
        events = wt / ".imp" / "change" / "events.jsonl"
        state_path = wt / ".imp" / "change" / "state" / "state.json"

        if not _wait_for_bash_tool_call(events, timeout=120.0):
            stdout, stderr = proc.communicate(timeout=10)
            pytest.fail(f"no bash; stdout={stdout!r} stderr={stderr!r}")
        time.sleep(2.0)  # CC subprocess startup is slower; extra grace
        os.killpg(proc.pid, signal.SIGINT)
        rc = proc.wait(timeout=20)
        assert rc == 130
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()

    state = json.loads(state_path.read_text())
    assert state["status"] == "paused"

    resume = subprocess.run(
        [sys.executable, "-m", "imp", "resume", slug],
        cwd=tmp_path, env=env,
        capture_output=True, text=True, timeout=240,
    )
    assert resume.returncode == 0, (
        f"resume failed rc={resume.returncode}; stderr={resume.stderr}"
    )
