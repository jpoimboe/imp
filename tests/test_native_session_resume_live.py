"""Resumed claude_code step does not redo prior reads (ckx9f5 regression).

Gated on IMP_LIVE_PROVIDERS=claude_code + the `claude` CLI on PATH. A multi-read
step is paused mid-bash and resumed; the prior reads must be recalled faithfully
(no '[Tool result missing…]' placeholder redo) and the resume must complete without
re-reading either file. A mid-bash SIGINT lands before any `result`, so the SDK
batcher has flushed no session bytes and the resume takes the stream-json fallback
(replay + Part-3 manifest); that manifest is what prevents the redo this asserts.
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

from tests._live import _await_change_dir, opted_in
from tests.conftest import _init_repo

_WORKTREE_SRC = str(Path(__file__).parent.parent / "src")


def _seed_multiread_project(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "a.txt").write_text("ALPHA-CONTENT\n")
    (tmp_path / "b.txt").write_text("BRAVO-CONTENT\n")
    wf = tmp_path / "imp" / "workflows" / "imp"
    (wf / "agents").mkdir(parents=True)
    (wf / "agents" / "writer.md").write_text(textwrap.dedent("""\
        ---
        name: writer
        description: Reads two files, runs a long bash, then completes.
        tools: [complete_step, bash, read_file]
        output:
          status: {type: enum, values: [Success, Error]}
        ---
        Do these steps in order, one tool call at a time:
        1. Read the file a.txt with read_file.
        2. Read the file b.txt with read_file.
        3. Run this shell command via bash: `sleep 30`.
        After bash returns (even if it was interrupted), call complete_step with
        status=Success. Do not read any file more than once.
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
    (tmp_path / "imp" / "project.yaml").write_text("default-workflow: imp\n")
    # providers/tiers go in the gitignored .imp/config.yaml local layer (highest
    # precedence) so a developer's ~/.config/imp/config.yaml cannot shadow them
    # and skip/fail this gated test — mirrors conftest._seed_control.
    (tmp_path / ".imp").mkdir(exist_ok=True)
    (tmp_path / ".imp" / "config.yaml").write_text(textwrap.dedent("""\
        providers: [claude_code]
        tiers:
          low: {claude_code: claude-haiku-4-5-20251001}
          medium: {claude_code: claude-haiku-4-5-20251001}
          high: {claude_code: claude-haiku-4-5-20251001}
    """))
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-qm", "imp setup"],
        cwd=tmp_path, check=True,
    )


def _iter_events(events_path: Path):
    if not events_path.exists():
        return
    try:
        body = events_path.read_text()
    except OSError:
        return
    for line in body.splitlines():
        if not line.strip():
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def _wait_for_bash_tool_call(events_path: Path, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        for rec in _iter_events(events_path):
            if (rec.get("type") == "tool_call"
                    and rec.get("payload", {}).get("name") == "bash"):
                return True
        time.sleep(0.2)
    return False


def _wait_for_status(state_path: Path, want: str, timeout: float) -> str:
    """Poll state.json until status == want (or timeout); return the last status.

    `imp new` runs the engine in a detached session daemon, so the SIGINT'd CLI
    client exits (rc 130) a beat before the daemon finishes persisting the pause.
    Reading state.json once right after proc.wait() races that write and can still
    see the running-status ("new"); poll for the settled status instead.
    """
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            last = json.loads(state_path.read_text()).get("status", "")
        except (OSError, json.JSONDecodeError):
            last = ""
        if last == want:
            return last
        time.sleep(0.2)
    return last


def _read_paths(events_path: Path) -> list[str]:
    """Every read_file tool_call's target path, in order.

    Spans the whole change lifecycle (both the initial run and the resume).
    """
    paths: list[str] = []
    for rec in _iter_events(events_path):
        if rec.get("type") != "tool_call":
            continue
        payload = rec.get("payload", {})
        if payload.get("name") != "read_file":
            continue
        path = payload.get("input", {}).get("path")
        if path is not None:
            paths.append(path)
    return paths


@pytest.mark.skipif(
    not opted_in("claude_code"),
    reason="claude_code not in IMP_LIVE_PROVIDERS",
)
def test_resumed_step_recalls_prior_reads_without_redo_or_400(
    tmp_path: Path,
) -> None:
    """One mid-bash SIGINT + `imp resume` covers both fallback-path invariants:

    * ckx9f5: the resume RECALLS the prior reads instead of redoing them (no
      '[Tool result missing…]' placeholder redo), so the whole-run read set stays
      exactly one a.txt + one b.txt.
    * Part C: replaying the deferred-MCP tool_use history does not trip `400 due
      to tool use concurrency issues` — the fallback sets ENABLE_TOOL_SEARCH=false
      so the MCP tools are declared up front rather than deferred behind
      ToolSearch, and the resume-manifest names the prior reads as succeeded so
      the is_error placeholders don't drive a redo.

    A mid-bash SIGINT lands before any `result`, so the SDK batcher flushed no
    session bytes and the resume takes the stream-json fallback (replay + Part-3
    manifest) that backs both invariants.
    """
    assert shutil.which("claude") is not None, (
        "claude_code opted in via IMP_LIVE_PROVIDERS but `claude` CLI "
        "not found on PATH"
    )
    _seed_multiread_project(tmp_path)
    env = os.environ.copy()
    env.pop("CLAUDECODE", None)
    _pypath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = _WORKTREE_SRC + (os.pathsep + _pypath if _pypath else "")

    proc = subprocess.Popen(
        [sys.executable, "-m", "imp", "new", "native-resume-regression"],
        cwd=tmp_path, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        change_dir = _await_change_dir(tmp_path, timeout=30.0)
        slug = change_dir.name
        events = change_dir / "events.jsonl"
        state_path = change_dir / "state" / "state.json"

        if not _wait_for_bash_tool_call(events, timeout=120.0):
            stdout, stderr = proc.communicate(timeout=10)
            pytest.fail(f"no bash; stdout={stdout!r} stderr={stderr!r}")
        # Give the model time to actually enter `sleep 30` before the interrupt.
        time.sleep(2.0)
        os.killpg(proc.pid, signal.SIGINT)
        # Generous: under `-n` concurrency the post-SIGINT exit stretches; this
        # only bites on a real hang.
        rc = proc.wait(timeout=60)
        assert rc == 130
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()

    status = _wait_for_status(state_path, "paused", timeout=30.0)
    assert status == "paused", status

    # Pre-resume sanity: both files were read before the mid-bash interrupt. The
    # imp-owned store is intentionally NOT asserted here — the SDK batcher flushes
    # only at a `result` boundary, so a mid-work interrupt legitimately leaves it
    # empty and the resume takes the stream-json fallback.
    pre = {Path(p).name for p in _read_paths(events)}
    assert {"a.txt", "b.txt"} <= pre, pre

    resume = subprocess.run(
        [sys.executable, "-m", "imp", "resume", slug],
        cwd=tmp_path, env=env,
        capture_output=True, text=True, timeout=240,
    )
    assert resume.returncode == 0, (
        f"resume failed rc={resume.returncode}; stderr={resume.stderr}"
    )
    final_state = json.loads(state_path.read_text())
    assert final_state["status"] == "completed"

    # Part C: the deferred-MCP-tool replay did NOT trip `400 due to tool use
    # concurrency issues` — the fallback sets ENABLE_TOOL_SEARCH=false so the
    # replayed MCP tool_use blocks are declared up front. (Assert the behavior,
    # not the manifest text, which lives in a user_message payload with an em-dash
    # that json.dumps would escape.)
    haystack = resume.stdout + resume.stderr + "\n".join(
        json.dumps(rec) for rec in _iter_events(events))
    assert "tool use concurrency" not in haystack, (
        "deferred-MCP-tool fallback replay tripped the 400 Part C fixes"
    )

    # ckx9f5 invariant: the resume recalled the prior reads faithfully instead of
    # redoing them, so the whole-run read set is still exactly one a.txt + one
    # b.txt. A placeholder-driven redo would re-read one or both → a count > 1.
    names = [Path(p).name for p in _read_paths(events)]
    assert names.count("a.txt") == 1, names
    assert names.count("b.txt") == 1, names
