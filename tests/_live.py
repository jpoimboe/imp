"""Gating helpers for live provider tests.

Live tests carry `@pytest.mark.live(<provider>)` and run only under
`--run-live` (see tests/conftest.py's collection hook and parse_run_live /
live_skip_reason / provider_available below). A selected provider whose
toolchain or credentials are absent skips (not fails) at collection.

The claude_code check resolves the CLI the SDK itself spawns -- its bundled
binary, then PATH, then the SDK's fixed locations -- not whatever `claude`
happens to be on PATH, which the SDK never consults. It is a presence check:
resolution only, no spawn.

A test that only launches a provider's CLI, with no model turn, belongs here
too: the launch itself is the live dependency. Measured on the claude CLI
(quest bepi35), its pre-init path -- node boot, an `rg --files` scan, a
`ps aux`, a git call, an awaited telemetry dial -- costs 15.9-16.2s with the
network degraded against 0.68-0.79s idle.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import NoReturn

import pytest

from imp.paths import project_state_dir
from tests.impty.e2e_support import E2E_EXIT_POLL_TIMEOUT

KNOWN_PROVIDERS: tuple[str, ...] = ("anthropic", "openai", "claude", "codex")

# Sentinel stored as the --run-live option's `const` (bare flag => all providers).
RUN_LIVE_ALL = object()


def parse_run_live(value: object) -> frozenset[str] | None:
    """Resolve the raw --run-live option value into the requested provider set.

    None  -> flag not passed (skip every live test).
    RUN_LIVE_ALL (bare --run-live) -> all known providers.
    "a,b" -> that subset; an unknown token is a pytest.UsageError.
    """
    if value is None:
        return None
    if value is RUN_LIVE_ALL:
        return frozenset(KNOWN_PROVIDERS)
    assert isinstance(value, str)
    tokens = [tok.strip() for tok in value.split(",") if tok.strip()]
    unknown = [tok for tok in tokens if tok not in KNOWN_PROVIDERS]
    if unknown:
        raise pytest.UsageError(
            "--run-live: unknown provider(s): "
            + ", ".join(unknown)
            + "; valid providers: "
            + ", ".join(KNOWN_PROVIDERS)
        )
    return frozenset(tokens)


def anthropic_key() -> str | None:
    """Resolve the anthropic key file-or-env: ~/.anthropic-key before the env
    var, so the conftest "sk-test" stub never shadows a real key."""
    key_file = Path.home() / ".anthropic-key"
    if key_file.exists():
        val = key_file.read_text().strip()
        if val:
            return val
    return os.environ.get("ANTHROPIC_API_KEY") or None


def openai_key() -> str | None:
    """Resolve the openai key file-or-env: ~/.openai-key before the env var."""
    key_file = Path.home() / ".openai-key"
    if key_file.exists():
        val = key_file.read_text().strip()
        if val:
            return val
    return os.environ.get("OPENAI_API_KEY") or None


def provider_available(provider: str) -> str | None:
    """None if the provider's toolchain + credentials are present on this
    machine, else a human-readable reason naming what is missing. claude_code
    resolves the binary the SDK will spawn, without spawning it."""
    if provider == "claude":
        from claude_agent_sdk import ClaudeAgentOptions, CLINotFoundError
        from claude_agent_sdk._internal.transport.subprocess_cli import (
            SubprocessCLITransport,
        )
        try:
            SubprocessCLITransport(prompt="", options=ClaudeAgentOptions())._find_cli()
        except CLINotFoundError:
            return "claude CLI not found (neither the SDK's bundled binary nor PATH)"
        return None
    if provider == "codex":
        if shutil.which("codex") is None:
            return "codex CLI not on PATH"
        return None
    if provider == "anthropic":
        return None if anthropic_key() else (
            "anthropic credentials not found "
            "(set ANTHROPIC_API_KEY or create ~/.anthropic-key)"
        )
    if provider == "openai":
        return None if openai_key() else (
            "openai credentials not found "
            "(set OPENAI_API_KEY or create ~/.openai-key)"
        )
    raise ValueError(f"unknown provider: {provider!r}")


def live_skip_reason(requested: frozenset[str] | None, provider: str) -> str | None:
    """Two-layer gate for one live-marked test. Returns a skip reason, or None
    to run. Layer 1: no --run-live -> skip all. Layer 2: not in the requested
    set -> skip as not-selected; requested but toolchain/creds missing -> skip
    with the capability reason."""
    if requested is None:
        return "live test — pass --run-live to run"
    if provider not in requested:
        return f"{provider} not selected by --run-live"
    return provider_available(provider)


_OVERLAY_MOUNT_SKIP_REASON = (
    'imp bash-sandbox overlay mount failed (rc 32: "wrong fs type … on '
    'overlay"); the writer\'s `sleep 30` never ran, so the '
    "mid-turn-interrupt precondition is unmet. This is a sandbox/environment "
    "failure, not an interrupt-resume regression."
)


def _bash_result_dicts(content: object):
    """Yield each parsed {stdout, stderr, returncode} dict from a bash
    tool_result `content`, which is EITHER a JSON string (the anthropic/openai
    in-process runner stores the bash JSON verbatim) OR a list of
    {"type": "text", "text": <json string>} parts (claude_code's SDK wraps it;
    see tests/fixtures/cc_tool_counter_burst.jsonl and the non-string branch in
    src/imp/engine/events_digest.py)."""
    texts: list[str] = []
    if isinstance(content, str):
        texts.append(content)
    elif isinstance(content, list):
        texts.extend(
            part["text"]
            for part in content
            if isinstance(part, dict)
            and part.get("type") == "text"
            and isinstance(part.get("text"), str)
        )
    for text in texts:
        try:
            result = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(result, dict):
            yield result


def sandbox_sleep_precondition_failed(events_path: Path) -> str | None:
    """Scan events.jsonl for the writer's bash `tool_result`. When it shows
    imp's per-command COW overlay home failing to mount (returncode 32 with the
    'wrong fs type … on overlay' / 'w.overlay-merged' signature in stderr), the
    scripted `sleep 30` never ran and the mid-turn-interrupt precondition is
    unmet — return a fixed descriptive skip reason. Otherwise return None.

    A `tool_result` payload carries no tool name, so bash results are found by
    correlating `payload["id"]` back to a preceding bash `tool_call`. The bash
    result JSON ({stdout, stderr, returncode}) is a string for the in-process
    providers and list-wrapped for claude_code — `_bash_result_dicts`
    normalises both shapes.
    """
    if not events_path.exists():
        return None
    try:
        body = events_path.read_text()
    except OSError:
        return None
    bash_ids: set[str] = set()
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        payload = rec.get("payload", {})
        if (rec.get("type") == "tool_call"
                and payload.get("name") in ("bash", "mcp__imp__bash")):
            tool_id = payload.get("id")
            if tool_id is not None:
                bash_ids.add(tool_id)
        elif rec.get("type") == "tool_result" and payload.get("id") in bash_ids:
            for result in _bash_result_dicts(payload.get("content")):
                stderr = result.get("stderr", "")
                if (result.get("returncode") == 32
                        and isinstance(stderr, str)
                        and "wrong fs type" in stderr
                        and "overlay" in stderr):
                    return _OVERLAY_MOUNT_SKIP_REASON
    return None


STALE_CODEX_OAUTH_SKIP_REASON = (
    "codex OAuth credential is stale (refresh token already used); run "
    "`codex login` to refresh it. External auth-infra failure, not a "
    "codex-provider regression."
)


def stale_codex_oauth_skip_reason(events) -> str | None:
    """Return the skip reason when the run's error events carry the codex
    stale-OAuth (refresh-token-reuse) signature, else None. Keys on imp's own
    "refresh token is dead" determination as well as the provider's prose: the
    upgraded auth_terminal error replaces the vendor text, and on the pre-spawn
    path there never was any. An external auth-infra condition, skip not fail."""
    for ev in events:
        if not isinstance(ev, dict) or ev.get("type") != "error":
            continue
        payload = ev.get("payload") or {}
        message = str(payload.get("message", "")).lower()
        if payload.get("category") in ("auth", "auth_terminal") and (
            "already used" in message
            or "log out and sign in" in message
            or "refresh token is dead" in message
        ):
            return STALE_CODEX_OAUTH_SKIP_REASON
    return None


_NATIVE_DENIAL_MARKERS = (
    # Measured verbatim wording on 0.153.2: "patch rejected: writing is
    # blocked by read-only sandbox; rejected by user approval settings"
    # and "TypeError: tools.exec_command is not a function".
    "read-only", "read only", "sandbox", "approval", "reject", "denied",
    "not permitted", "permission", "not a function", "not defined",
    # Natural-language paraphrase of the same denial.
    "not available", "unavailable", "not accessible", "no access",
    "not have access", "n't have access", "not exposed", "not bound",
    "blocked", "refus", "forbidden", "not allowed", "not supported",
)

# Widening to "not available" alone would start accepting a report from a model
# that never tried, so any of these vetoes a leg even when a denial marker also
# matched — this is what keeps the widened check discriminating.
_NEVER_ATTEMPTED_MARKERS = (
    "no error — it succeeded", "no error - it succeeded",
    "did not attempt", "didn't attempt", "did not try", "didn't try",
    "skipped", "chose not to",
)


def _is_denial_evidence(report: str) -> bool:
    return (any(m in report for m in _NATIVE_DENIAL_MARKERS)
            and not any(m in report for m in _NEVER_ATTEMPTED_MARKERS))


def native_denial_miss(patch_error: str, exec_error: str) -> str | None:
    """Return None when both self-reported legs carry native-denial evidence,
    else a one-line description of what is missing. Matching is
    case-insensitive: callers pass the raw payload strings."""
    patch_error = patch_error.lower()
    exec_error = exec_error.lower()
    misses: list[str] = []
    if not _is_denial_evidence(patch_error):
        misses.append(
            f"patch_error={patch_error!r} carries no denial language (the model "
            f"likely never attempted the native apply_patch)")
    if not _is_denial_evidence(exec_error):
        misses.append(
            f"exec_error={exec_error!r} carries no denial language (the model "
            f"likely never attempted the native shell write)")
    return "; ".join(misses) or None


def build_isolated_venv(
    project_root: Path,
    *,
    python: str | None = None,
    extras: tuple[str, ...] = (),
) -> Path:
    """Build an ISOLATED venv (NO --system-site-packages) under
    project_root/.venv and install project_root editable + its deps into it.

    Prefers `uv` for speed; falls back to stdlib venv + pip. No
    --system-site-packages means the host's editable `imp` cannot shadow the
    copy: the venv sees only what is installed here.

    `extras` names optional-dependency groups (e.g. ("dev",)) to install with
    the editable target — the isolated venv has no host site-packages to borrow
    from, so test tools like pytest must be installed here explicitly. Empty by
    default so the stub-isolation test keeps a deliberately bare venv.

    Returns the venv's python interpreter path.
    """
    import shutil

    venv_dir = project_root / ".venv"
    base_python = python or sys.executable
    target = (
        f"{project_root}[{','.join(extras)}]" if extras else str(project_root)
    )

    if shutil.which("uv"):
        # uv's cache (~/.cache/uv) and the venv (often tmpfs /tmp) sit on
        # different filesystems, so its default hardlink install can't work —
        # it falls back to copying and prints a "Failed to hardlink files"
        # warning on every seed. Ask for copy mode up front to silence it.
        uv_env = {**os.environ, "UV_LINK_MODE": "copy"}
        subprocess.run(
            ["uv", "venv", "--python", base_python, str(venv_dir)],
            check=True, env=uv_env,
        )
        venv_python = venv_dir / "bin" / "python"
        subprocess.run(
            ["uv", "pip", "install", "--python", str(venv_python),
             "-e", target],
            check=True, env=uv_env,
        )
    else:
        subprocess.run([base_python, "-m", "venv", str(venv_dir)], check=True)
        venv_python = venv_dir / "bin" / "python"
        subprocess.run(
            [str(venv_python), "-m", "pip", "install",
             "-e", target, "-q"],
            check=True,
        )
    return venv_python


def _await_worktree(control_root: Path, timeout: float) -> Path:
    """Poll the state dir's worktrees/ for the single Quest worktree dir and
    return it; its .name is the slug. The real guard is the presence of
    quests/<slug>/quest.json in the state dir (scratch/.merge-* dirs
    have no such entry); the leading-dot exclusion is redundant belt-and-suspenders.

    Replaces stdout-parsing of `imp new`'s "Created Quest <slug>:" line:
    sidesteps PTY/PIPE read-timing and yields the slug directly. Raises
    TimeoutError if no Quest worktree appears within `timeout` seconds."""
    root = project_state_dir(control_root) / "worktrees"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if root.is_dir():
            for p in sorted(root.iterdir()):
                if (p.is_dir() and not p.name.startswith(".")
                        and (project_state_dir(control_root) / "quests" / p.name
                             / "quest.json").exists()):
                    return p
        time.sleep(0.1)
    raise TimeoutError(
        f"no Quest worktree appeared under {root} within {timeout}s"
    )


def _await_change_dir(control_root: Path, timeout: float) -> Path:
    """Poll the state dir's quests/ for the single Quest's permanent home dir
    and return it;
    its .name is the slug. Guards on quest.json so a half-written dir is skipped.

    Under the deferred-worktree model `imp new` creates an un-materialized draft
    (no branch/worktree) with state+events under the state dir's quests/<id>/.
    A workflow that never calls create_and_enter_worktree runs to completion as a
    draft, so tests that used to await a worktree must await the change home here.
    Raises TimeoutError if no Quest home appears within `timeout` seconds."""
    root = project_state_dir(control_root) / "quests"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if root.is_dir():
            for p in sorted(root.iterdir()):
                if p.is_dir() and (p / "quest.json").exists():
                    return p
        time.sleep(0.1)
    raise TimeoutError(
        f"no Quest home appeared under {root} within {timeout}s"
    )


def wait_for_archived_status(
    control_root: Path, quest_id: str, want: str, timeout: float
) -> str | None:
    """Poll the archive for a quest's terminal status after its live home is reaped.

    A no-merge draft completion finishes `done`, archives the terminal snapshot,
    then rmtrees the live home. The CLI client returns before the detached daemon
    finishes that archive+reap, so poll the archived state.json for the settled
    status instead of the (about-to-vanish) live home."""
    from imp.engine.archive import read as archive_read
    deadline = time.time() + timeout
    last: str | None = None
    while time.time() < deadline:
        last = archive_read.status_of(control_root, quest_id)
        if last == want:
            return last
        time.sleep(0.2)
    return last


_EVIDENCE_TAIL_LINES = 40


def _tail(path: Path, lines: int = _EVIDENCE_TAIL_LINES) -> str:
    if not path.exists():
        return f"<no {path.name}>"
    try:
        return "\n".join(path.read_text(errors="replace").splitlines()[-lines:])
    except OSError as exc:
        return f"<unreadable {path.name}: {exc}>"


def evidence(quest_dir: Path) -> str:
    """Failure evidence for a live subprocess run. events.jsonl carries the real
    trace; daemon.stderr is empty unless the daemon itself crashed."""
    return (
        f"events.jsonl tail:\n{_tail(quest_dir / 'events.jsonl')}\n"
        f"daemon.stderr tail:\n{_tail(quest_dir / 'daemon.stderr')}"
    )


def fail_no_bash_tool_call(
    proc: subprocess.Popen, quest_dir: Path, waited: float
) -> NoReturn:
    """Kill the client BEFORE draining: on this path `imp new` has not exited, so a
    bare communicate() only ever raises TimeoutExpired and buries the diagnostic.
    The detached daemon holds neither pipe, so the drain returns once the client
    dies (measured 0.004s)."""
    if proc.poll() is None:
        proc.kill()
    out, err = proc.communicate(timeout=E2E_EXIT_POLL_TIMEOUT)
    pytest.fail(
        f"never saw bash tool_call within {waited}s\n"
        f"{evidence(quest_dir)}\n"
        f"client stdout:\n{out.decode(errors='replace')}\n"
        f"client stderr:\n{err.decode(errors='replace')}"
    )


def assert_sigint_exit(proc: subprocess.Popen, quest_dir: Path, timeout: float) -> None:
    """A bare wait() raises a content-free TimeoutExpired when the client hangs."""
    try:
        rc = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        pytest.fail(f"client never exited within {timeout}s of SIGINT\n{evidence(quest_dir)}")
    if rc != 130:
        pytest.fail(f"expected exit 130, got {rc}\n{evidence(quest_dir)}")


def wait_for_bash_tool_call(events_path: Path, timeout: float) -> bool:
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


def wait_for_status(state_path: Path, want: str, timeout: float) -> str:
    """Poll state.json until status == want (or timeout); return the last status.

    Both `imp new` and `imp resume` run the engine in a detached session daemon,
    so the CLI client exits a beat before the daemon finishes persisting the
    settled status: the SIGINT'd `imp new` client exits (rc 130) before the pause
    is written, and the `imp resume` client can return before the daemon persists
    `completed`. Reading state.json once right after the client exits races that
    write and can still see a running status; poll for the settled status instead.
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
