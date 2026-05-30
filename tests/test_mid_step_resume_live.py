"""Mid-step Ctrl+C resume against real provider APIs.

Verifies §6.15 mechanics end-to-end:
  1. Spawn `imp new` as a subprocess.
  2. Wait for the writer's bash subprocess to start (poll for the
     events.jsonl `tool_call` event named "bash").
  3. Send SIGINT to the imp subprocess.
  4. Confirm the imp subprocess exits cleanly with code 130.
  5. Confirm state.json is "paused" with the writer step still set.
  6. Spawn `imp resume <id>`; confirm completion.

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

from tests._live import opted_in


def _anthropic_key() -> str | None:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key
    key_file = Path.home() / ".anthropic-key"
    if key_file.exists():
        return key_file.read_text().strip()
    return None


def _seed_project(tmp_path: Path) -> None:
    wf = tmp_path / ".imp" / "workflows" / "default"
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
              - {name: w, file: agents/writer.md, model_role: default}
    """))
    (tmp_path / ".imp" / "config.yaml").write_text(textwrap.dedent("""\
        roles:
          default:
            prefer: [{provider: anthropic, model: claude-sonnet-4-6}]
    """))


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
        # The change_id is 1 (fresh tmp project).
        events = tmp_path / ".imp" / "changes" / "1" / "events.jsonl"
        if not _wait_for_bash_tool_call(events, timeout=60.0):
            stdout, stderr = proc.communicate(timeout=10)
            pytest.fail(
                f"never saw bash tool_call event; stdout={stdout!r} stderr={stderr!r}"
            )
        # Wait briefly so bash has settled into `sleep 30`, then SIGINT.
        time.sleep(1.0)
        os.killpg(proc.pid, signal.SIGINT)
        rc = proc.wait(timeout=15)
        # exit 130 = ctrl-c convention used in main.py
        assert rc == 130, f"expected exit 130, got {rc}"

        # Verify state is paused.
        state_path = tmp_path / ".imp" / "changes" / "1" / "state" / "state.json"
        state = json.loads(state_path.read_text())
        assert state["status"] == "paused"
        assert state["step"] == "w"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()

    # Resume.
    resume = subprocess.run(
        [sys.executable, "-m", "imp", "resume", "1"],
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
    (tmp_path / ".imp" / "config.yaml").write_text(textwrap.dedent("""\
        roles:
          default:
            prefer: [{provider: openai, model: gpt-4o-mini}]
    """))
    env = os.environ.copy()
    key = _openai_key()
    assert key is not None
    env["OPENAI_API_KEY"] = key
    env.pop("CLAUDECODE", None)

    proc = subprocess.Popen(
        [sys.executable, "-m", "imp", "new", "mid-step-resume-oa"],
        cwd=tmp_path, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        events = tmp_path / ".imp" / "changes" / "1" / "events.jsonl"
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

    state_path = tmp_path / ".imp" / "changes" / "1" / "state" / "state.json"
    state = json.loads(state_path.read_text())
    assert state["status"] == "paused"

    resume = subprocess.run(
        [sys.executable, "-m", "imp", "resume", "1"],
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
    (tmp_path / ".imp" / "config.yaml").write_text(textwrap.dedent("""\
        roles:
          default:
            prefer: [{provider: claude_code, model: claude-sonnet-4-5}]
    """))
    env = os.environ.copy()
    env.pop("CLAUDECODE", None)

    proc = subprocess.Popen(
        [sys.executable, "-m", "imp", "new", "mid-step-resume-cc"],
        cwd=tmp_path, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        events = tmp_path / ".imp" / "changes" / "1" / "events.jsonl"
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

    state_path = tmp_path / ".imp" / "changes" / "1" / "state" / "state.json"
    state = json.loads(state_path.read_text())
    assert state["status"] == "paused"

    resume = subprocess.run(
        [sys.executable, "-m", "imp", "resume", "1"],
        cwd=tmp_path, env=env,
        capture_output=True, text=True, timeout=240,
    )
    assert resume.returncode == 0, (
        f"resume failed rc={resume.returncode}; stderr={resume.stderr}"
    )
