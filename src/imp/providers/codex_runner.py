"""CodexAgentRunner — the heavy lifting for the `codex` provider.

Implements: interrupt() with three effects, fail-fast preflight(),
snapshot/restore mechanism (git stash create + untracked set), and
process_event() for basic event types. The full run() is added in later tasks.
"""

from __future__ import annotations

import contextlib
import shutil
import subprocess
from collections.abc import AsyncGenerator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from imp.agents.runner import AgentInvocation
from imp.agents.tools.context import ToolContext, terminate_running_processes
from imp.providers.codex_config import AuthMode, detect_auth_mode

# ── PreflightError ────────────────────────────────────────────────────────────


class PreflightError(Exception):
    """Raised before SDK options are built when the run cannot proceed safely."""
    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category


def _is_git_repo(cwd: Path) -> bool:
    """True iff `cwd` is inside a git working tree."""
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=str(cwd), check=False, capture_output=True, text=True, timeout=5.0,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0 and r.stdout.strip() == "true"


def preflight(
    *, model: str, cwd: Path, canonical_auth: Path, env: Mapping[str, str],
) -> AuthMode:
    """Run fail-fast checks before building SDK options. Returns auth mode."""
    # 1. Refuse `*-codex` in BOTH auth modes (M1).
    if model.endswith("-codex"):
        raise PreflightError(
            "config",
            f"model {model!r} is a *-codex variant; codex provider refuses "
            "*-codex in both auth modes until it gets fail-closed obedience "
            "validation. Choose a non-codex model (e.g. gpt-5.5).",
        )
    # 2. Non-git cwd → no snapshot substrate (M2).
    if not _is_git_repo(cwd):
        raise PreflightError(
            "config",
            f"cwd {cwd!s} is not a git repository; codex provider requires "
            "git for the fail-closed snapshot/revert mechanism.",
        )
    # 3. Auth must exist.
    mode = detect_auth_mode(canonical_path=canonical_auth, env=env)
    if mode is None:
        raise PreflightError(
            "auth",
            "no usable codex auth (neither ~/.codex/auth.json nor "
            "OPENAI_API_KEY); run `codex login` or export an API key.",
        )
    return mode


# ── EntrySnapshot / restore ───────────────────────────────────────────────────

@dataclass(frozen=True)
class EntrySnapshot:
    """Captured state at `run()` entry — used to revert on fail-closed."""
    stash_sha: str
    untracked: frozenset[str] = field(default_factory=frozenset)


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True,
        capture_output=True, text=True,
    )


def take_snapshot(cwd: Path) -> EntrySnapshot:
    """Snapshot tracked state + record untracked set at run() entry."""
    stash_sha = _git(cwd, "stash", "create").stdout.strip()
    untracked_raw = _git(cwd, "ls-files", "--others", "--exclude-standard").stdout
    untracked = frozenset(line for line in untracked_raw.splitlines() if line)
    return EntrySnapshot(stash_sha=stash_sha, untracked=untracked)


def restore_to_snapshot(cwd: Path, snap: EntrySnapshot) -> None:
    """Restore the worktree to the entry snapshot."""
    # 1. Tracked state.
    # Unstage any pre-existing untracked files that got staged during the run,
    # so that `git reset --hard HEAD` does not delete them.
    if snap.untracked:
        staged_raw = subprocess.run(
            ["git", "ls-files", "--cached", "--"],
            cwd=str(cwd), capture_output=True, text=True, check=False,
        ).stdout
        staged = set(staged_raw.splitlines())
        to_unstage = [f for f in snap.untracked if f in staged]
        if to_unstage:
            _git(cwd, "rm", "--cached", "--force", "--", *to_unstage)
    _git(cwd, "reset", "--hard", "HEAD")
    if snap.stash_sha:
        try:
            _git(cwd, "stash", "apply", "--index", snap.stash_sha)
        except subprocess.CalledProcessError:
            _git(cwd, "stash", "apply", snap.stash_sha)
    # 2. Remove this-run-only untracked files.
    current_raw = _git(cwd, "ls-files", "--others", "--exclude-standard").stdout
    current = [line for line in current_raw.splitlines() if line]
    for rel in current:
        if rel in snap.untracked:
            continue
        target = cwd / rel
        with contextlib.suppress(OSError):
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target, ignore_errors=True)
            else:
                target.unlink(missing_ok=True)


# ── CodexAgentRunner ──────────────────────────────────────────────────────────

class CodexAgentRunner:
    """Drives `codex` via `openai_codex_sdk` (run() filled in by later tasks)."""

    def __init__(
        self, model_name: str, *,
        client_factory: Any = None,
        user_input_fn: Any = None,
    ) -> None:
        self.model = model_name
        self._client_factory = client_factory
        self._user_input_fn = user_input_fn
        self._cancel = False
        self._ctx: ToolContext | None = None
        self._abort_controller: Any = None

    async def run(self, invocation: AgentInvocation) -> AsyncGenerator[Any, None]:
        # Filled in by Tasks 27+.
        raise NotImplementedError("CodexAgentRunner.run() not yet implemented")
        yield {}  # type: ignore[unreachable]  # pragma: no cover

    async def interrupt(self) -> None:
        """Cancel the run — three effects:
          1. set `self._cancel = True`.
          2. `terminate_running_processes(ctx, timeout=2.0)`.
          3. SDK `AbortController.abort()`.
        Idempotent."""
        self._cancel = True
        if self._ctx is not None:
            await terminate_running_processes(self._ctx, timeout=2.0)
        if self._abort_controller is not None:
            with contextlib.suppress(Exception):
                self._abort_controller.abort()


# ── Event mapping ─────────────────────────────────────────────────────────────


def process_event(
    event: Any, *, elog: Any, agent_id: str, state: dict[str, Any],
    provider: str, model: str,
) -> None:
    """Translate one codex ThreadEvent into imp EventLog records.

    `state` is a shared dict carrying in-flight thread_id, pending usage,
    captured complete_step args, etc. Filled in across subsequent tasks.

    The default arm (unknown item type) logs a runner_warning and returns
    (L3 invariant); only security-relevant types get special handling.
    """
    etype = getattr(event, "type", None)

    # `thread.started` — capture thread_id for resume / re-query.
    if etype == "thread.started":
        state["thread_id"] = getattr(event, "thread_id", None)
        return

    # `item.completed` — dispatch on the item's own type.
    # `item.started` is intentionally left to fall through (no final content).
    if etype == "item.completed":
        item = getattr(event, "item", None)
        itype = getattr(item, "type", None)
        if itype == "reasoning":
            content = getattr(item, "text", "") or ""
            elog.append(agent_id=agent_id, type="thinking",
                        payload={"content": content})
            return
        if itype == "agent_message":
            text = getattr(item, "text", "") or ""
            elog.append_llm_response(
                agent_id=agent_id, provider=provider, model=model,
                raw={"content": [{"type": "text", "text": text}]},
                usage={}, stop_reason=None,
                auth_mode=state.get("auth_mode"),
            )
            return
        # Other item types mapped in later tasks; fall through to default arm.
        elog.append(agent_id=agent_id, type="runner_warning",
                    payload={"message": f"unhandled codex item type: {itype!r}"})
        return

    # Default arm: turn.completed / turn.failed / error / unknown —
    # handled by subsequent tasks. Falling through here is benign.
