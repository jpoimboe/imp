"""CodexAgentRunner — the heavy lifting for the `codex` provider.

Implements: interrupt() with four effects, fail-fast preflight(),
snapshot/restore mechanism (git stash create + untracked set), and
process_event() for basic event types.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import secrets
import shutil
import subprocess
import tempfile
from collections.abc import AsyncGenerator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from imp.agents.loop_guard import LoopGuard, ToolOutcome, TurnRecord, _result_preview
from imp.agents.runner import (
    AgentInvocation,
    Diagnostics,
    Event,
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from imp.agents.subagents import raise_if_suspended
from imp.agents.tools import bg as _bg
from imp.agents.tools.context import ToolContext, change_home_path, terminate_running_processes
from imp.agents.tools.wait_for_user import AwaitingUserInput
from imp.engine.agent_run import _resolve_wait_reply
from imp.engine.schema import validate_agent_output
from imp.engine.state import JsonlEventLog
from imp.engine.types import AgentDef
from imp.providers import errors as perr
from imp.providers.codex_config import (
    AuthMode,
    detect_auth_mode,
    provision_codex_home,
    seed_auth,
    sweep_stale_codex_homes,
    teardown_codex_home,
)
from imp.providers.codex_mcp import (
    CODEX_APPROVAL_POLICY,
    RunState,
    ServerHandle,
    start_impty_server,
    start_server,
)

_logger = logging.getLogger(__name__)

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
        # Test seam: overrides the canonical ~/.codex/auth.json path. Read by
        # `_canonical_auth_path()`; declared here so the attribute the tests set
        # is part of the class contract.
        self._canonical_auth_override: Path | None = None
        # Per-step capture of the nested `codex` CLI's stderr. Built in _run_loop
        # and published to current_stderr_sink for the subprocess-patch tee. None
        # until built (the stubbed-loop outer-except tests never build it).
        self._stderr_sink: Any = None

    async def run(self, invocation: AgentInvocation) -> AsyncGenerator[Event, None]:
        try:
            try:
                async for ev in self._run_loop(invocation):
                    yield ev
            except PreflightError as exc:
                yield {"type": "error", "payload": {
                    "category": exc.category, "kind": "preflight",
                    "message": str(exc), "detail": {}}}
                return
            except perr.ProviderError as exc:
                yield perr.make_error_event(exc)
                return
            except AwaitingUserInput:
                raise  # bare-turn-end / resume-solicit pause unwinds, not classified
            except Exception as exc:
                raise_if_suspended(exc)  # subagent-origin pause must unwind, not classify
                if self._cancel:
                    yield {"type": "error", "payload": {
                        "category": "config", "kind": "interrupted",
                        "message": "runner cancelled by interrupt"}}
                    return
                tail = None
                if self._stderr_sink is not None:
                    # Prefer what the streaming tee captured live. Only when it
                    # captured nothing do we fall back to str(exc): the codex SDK
                    # surfaces subprocess stderr solely inside the CodexExecError
                    # message (no .stderr attribute), which also re-embeds any
                    # already-teed stderr and already lands in the event's
                    # `message` — so feeding it unconditionally would duplicate.
                    tail = self._stderr_sink.tail()
                    if not tail:
                        self._stderr_sink.feed(str(exc))
                        tail = self._stderr_sink.tail()
                yield perr.make_unknown_error_event(
                    exc, detail={"stderr_tail": tail} if tail else None)
                return
        finally:
            if self._ctx is not None:
                # None guard: _ctx is assigned mid-_run_loop (after preflight).
                # run()'s OWN finally, not _run_loop's existing one — an
                # abandoned inner generator only GC-finalizes (PEP 525), so
                # the MCP/CODEX_HOME finally there is not aclose-deterministic
                # and bg teardown must not depend on it.
                from imp.agents.tools.bg import terminate_background_processes
                with contextlib.suppress(Exception):
                    await terminate_background_processes(self._ctx)

    async def _run_loop(
        self, invocation: AgentInvocation,
    ) -> AsyncGenerator[Event, None]:
        elog = invocation.event_log or JsonlEventLog(invocation.events_path)
        aid = invocation.agent_id or "step:unnamed"

        # 1. Preflight (auth / *-codex / non-git cwd) — raises PreflightError.
        canonical_auth = self._canonical_auth_path()
        cwd = invocation.change_context.project_root
        auth_mode = preflight(
            model=self.model, cwd=cwd, canonical_auth=canonical_auth,
            env=os.environ,
        )

        # 2. Build tools + ToolContext (mirror the claude_code runner exactly).
        from imp.providers.claude_code import _build_tools
        from imp.providers.resolve import spawn_subagent_via_registry
        tools = _build_tools(invocation)
        ctx = ToolContext(
            project_root=cwd,
            events_path=invocation.events_path,
            subagent_defs=invocation.subagent_defs,
            provider_name=invocation.provider_name,
            model_name=invocation.model_name,
            parent_transcript_dir=invocation.transcript_dir,
            agent_id=aid,
            background_enabled=True,  # full bg surface wired: injection + sweeps + teardown
            change_home=change_home_path(invocation.change_context.change_home),
        )
        ctx.change_context = invocation.change_context
        ctx.change_state = invocation.change_state
        ctx.change = invocation.change          # live Change for rename_change
        ctx.spawn_subagent = spawn_subagent_via_registry
        ctx.resolved_tiers = invocation.resolved_tiers
        ctx.subagent_depth = invocation.subagent_depth
        ctx.coordinator = invocation.coordinator
        ctx.tiers = invocation.tiers
        ctx.verify_command = invocation.verify_command
        ctx.non_interactive = invocation.non_interactive
        ctx.sandbox_policy = invocation.sandbox_policy
        ctx.event_log = elog
        if self._user_input_fn is not None:
            ctx.user_input_fn = self._user_input_fn
        self._ctx = ctx

        state = RunState(cap=invocation.max_tool_calls or 1000, agent_id=aid)
        state.runner_loop = asyncio.get_running_loop()
        # Shared event-processing state (process_event keys).
        proc_state: dict[str, Any] = {
            "thread_id": None, "complete_step_args": None,
            "complete_step_called": False, "native_violation": None,
            "auth_mode": auth_mode, "pending_usage": None,
        }

        # 3. Stale-CODEX_HOME sweep (best-effort).
        tmp_root = Path(tempfile.gettempdir())
        sweep_stale_codex_homes(tmp_root=tmp_root, max_age_seconds=24 * 3600)

        # 4. Take entry snapshot for the fail-closed revert mechanism.
        entry_snapshot = take_snapshot(cwd)

        # 5. Start the in-process MCP server on an ephemeral port.
        bearer_token = secrets.token_urlsafe(24)
        server_handle = await start_server(
            tools=tools, ctx=ctx, state=state,
            bearer_token=bearer_token, host="127.0.0.1",
        )

        # 5a..8. Everything after the server starts runs inside ONE try/finally
        # so the MCP server is ALWAYS shut down and any provisioned CODEX_HOME
        # is ALWAYS torn down — even if the ping, provision, or seed_auth step
        # raises (Task 31: close the provision/seed teardown leak window).
        from imp.providers.stderr_sink import StderrSink, current_stderr_sink
        self._stderr_sink = StderrSink(invocation.transcript_dir / "stderr.log")
        _stderr_token = current_stderr_sink.set(self._stderr_sink)
        codex_home: Path | None = None
        impty_handle: ServerHandle | None = None
        impty_driver: Any = None
        try:
            # 5a. Imp-side server-start ping: a single bearer-auth'd HTTP probe
            # against `server_handle.url` to fail fast before provisioning if
            # imp can't reach its own server. On failure we just return; the
            # finally below shuts the server down (codex_home is still None, so
            # teardown_codex_home is correctly skipped).
            try:
                await _ping_mcp_server(
                    url=server_handle.url, bearer_token=bearer_token,
                    timeout_seconds=2.0,
                )
            except Exception as ping_exc:
                yield perr.make_error_event(perr.NetworkError(
                    (
                        f"imp-side MCP server-start ping failed; codex would "
                        f"not be able to reach the MCP endpoint either. "
                        f"({type(ping_exc).__name__}: {ping_exc})"),
                    detail={"url": server_handle.url},
                ))
                return

            # 6. Provision CODEX_HOME + config.toml + seed auth. web_search is
            #    controlled via config.toml's top-level key (the deprecated SDK
            #    flag no longer disables it on codex >=0.137), so it must be
            #    decided here, at provision time, from the agent's declared tools.
            impty_bearer: str | None = None
            if "impty" in invocation.agent_def.drivers:
                from impty.driver import ImptyDriver
                impty_bearer = secrets.token_urlsafe(24)
                impty_driver = ImptyDriver()
                impty_handle = await start_impty_server(
                    driver=impty_driver, bearer_token=impty_bearer,
                    host="127.0.0.1", runner_loop=state.runner_loop,
                )
            from imp.providers.web_native import declared_web_tools
            web_enabled = "web_search" in declared_web_tools(invocation.agent_def)
            codex_home = provision_codex_home(
                tmp_root=tmp_root, host=server_handle.host,
                port=server_handle.port, bearer_env_var="IMP_MCP_TOKEN",
                web_search_enabled=web_enabled,
                impty_url=impty_handle.url if impty_handle is not None else None,
                impty_bearer_env_var=(
                    "IMP_IMPTY_TOKEN" if impty_handle is not None else None),
            )
            seed_auth(mode=auth_mode, canonical_path=canonical_auth,
                      codex_home=codex_home, env=os.environ)

            # 7. Run the SDK turn(s) and consume events.
            client = self._build_client(
                codex_home=codex_home, bearer_token=bearer_token,
                impty_bearer_token=(impty_bearer if impty_handle is not None
                                    else None))
            async for ev in self._drive_turns(
                client=client, invocation=invocation, elog=elog,
                aid=aid, ctx=ctx, state=state, proc_state=proc_state,
                entry_snapshot=entry_snapshot, server_handle=server_handle,
                codex_home=codex_home,
            ):
                yield ev
        finally:
            # 8. Lifecycle teardown — runs on EVERY exit (success, error,
            #    abort, interrupt, AND a ping/provision/seed failure). The
            #    server shutdown ALWAYS runs; CODEX_HOME teardown runs only when
            #    a codex_home was actually provisioned.
            current_stderr_sink.reset(_stderr_token)
            self._stderr_sink.close()
            if impty_handle is not None:
                with contextlib.suppress(Exception):
                    await impty_handle.shutdown()
            if impty_driver is not None:
                with contextlib.suppress(Exception):
                    impty_driver.close()
            await server_handle.shutdown()
            if codex_home is not None:
                # Own codex's native rollout across the pause BEFORE teardown
                # deletes CODEX_HOME (Fix 1). Best-effort: a persist failure must
                # not skip the auth-shredding teardown below. A later resume that
                # finds no persisted rollout fails loudly in restore_rollout.
                tid = proc_state.get("thread_id")
                if tid:
                    from imp.providers.codex_rollout import persist_rollout
                    try:
                        persist_rollout(
                            codex_home=codex_home, thread_id=tid,
                            transcript_dir=invocation.transcript_dir,
                        )
                    except Exception as exc:  # pragma: no cover - defensive
                        _logger.warning(
                            "codex: rollout persist failed (resume may be "
                            "unavailable): %s", exc)
                teardown_codex_home(
                    codex_home=codex_home, mode=auth_mode,
                    canonical_path=canonical_auth,
                    lock_path=canonical_auth.parent / ".imp-auth.lock",
                )

    def _canonical_auth_path(self) -> Path:
        """The user's real `~/.codex/auth.json`. Test seam: a runner attribute
        can override this for the auth fixture tests."""
        override = getattr(self, "_canonical_auth_override", None)
        if override is not None:
            return Path(override)
        return Path.home() / ".codex" / "auth.json"

    def _build_client(
        self, *, codex_home: Path, bearer_token: str,
        impty_bearer_token: str | None = None,
    ) -> Any:
        """Construct the SDK `Codex` client. Tests inject a `client_factory`
        callable that takes `options=` and returns a FakeCodex."""
        if self._client_factory is not None:
            return self._client_factory(options=None)
        from openai_codex_sdk import Codex

        from imp.providers.codex_subprocess_patch import install_codex_subprocess_patches
        install_codex_subprocess_patches()

        # `CodexOptions` is not re-exported at the package top level in the
        # pinned SDK (0.1.11) — only `openai_codex_sdk.types` (and .codex/.thread)
        # expose it. Import from the submodule so client construction works
        # against the pinned wheel. `Codex(...)` also accepts a plain dict, but
        # the typed object keeps the kwarg contract explicit.
        from openai_codex_sdk.types import CodexOptions

        from imp.engine.env import overlay_engine_env
        overrides = {
            "CODEX_HOME": str(codex_home),
            "IMP_MCP_TOKEN": bearer_token,
        }
        if impty_bearer_token is not None:
            overrides["IMP_IMPTY_TOKEN"] = impty_bearer_token
        env = overlay_engine_env(os.environ, overrides)
        return Codex(CodexOptions(env=env))

    async def _drive_turns(
        self, *, client: Any, invocation: AgentInvocation, elog: Any,
        aid: str, ctx: ToolContext, state: RunState, proc_state: dict[str, Any],
        entry_snapshot: EntrySnapshot, server_handle: Any, codex_home: Path,
    ) -> AsyncGenerator[Event, None]:
        """Start a thread, run streamed turns, consume events.

        Multi-turn nudge loop (Task 28): on a turn that completes without
        `complete_step`, classified provider-error text short-circuits; a run
        with a wired `continuation_fn` is nudged for another turn; exhausted
        runs emit the appropriate terminal error. Inline cap-abort /
        native-violation / turn.failed / interrupt handling is preserved
        verbatim inside the inner loop.
        """
        # Enable codex's model-hosted web_search ONLY when this agent declared
        # the tool (governance: agents opt in via `tools:`). The flag also gates
        # how process_event surfaces a web_search item (expected tool_call vs
        # disabled-state tripwire).
        from imp.providers.web_native import declared_web_tools
        web_enabled = "web_search" in declared_web_tools(invocation.agent_def)
        proc_state["web_search_enabled"] = web_enabled
        thread_options = self._build_thread_options()
        if invocation.balance_on_resume:
            # Resume edge (Fix 1): restore codex's rollout into the fresh
            # CODEX_HOME, then resume the native thread by id instead of starting
            # a new one. restore_rollout fails loudly if the rollout is missing.
            from imp.providers.codex_rollout import restore_rollout
            thread_id = restore_rollout(
                codex_home=codex_home,
                transcript_dir=invocation.transcript_dir)
            # Seed thread_id now so a subsequent pause persists the resumed
            # thread even if the SDK does not re-emit thread.started on resume
            # (process_event overwrites this with the same id if it does).
            proc_state["thread_id"] = thread_id
            if thread_options is None:
                thread = client.resume_thread(thread_id)
            else:
                try:
                    thread = client.resume_thread(thread_id, thread_options)
                except TypeError:
                    thread = client.resume_thread(thread_id)
        elif thread_options is None:
            # SDK unavailable (test/fake path): start with no options.
            thread = client.start_thread()
        else:
            try:
                thread = client.start_thread(thread_options)
            except TypeError:
                # Fakes ignore the options arg.
                thread = client.start_thread()
        # Build initial prompt with anti-native-tool instruction prepended.
        next_prompt = self._build_prompt(invocation.system_prompt)
        # Hoist rstate so the §2 resume-entry drain below can reference it safely
        # even when balance_on_resume is False (fresh-entry path has no rstate).
        rstate = None
        # Resume seam (Unit G): on a balance_on_resume re-entry, codex holds no
        # parsed message list, so derive the resume state from imp's own
        # events.jsonl. An AWAITING_USER turn (a bare-turn-end yield, detected
        # via the journaled awaiting_user_input event) solicits the human reply
        # via user_input_fn and injects it as the FIRST resumed prompt instead of
        # the system prompt; MISSING_COMPLETE_STEP (autonomous, no complete_step)
        # triggers replay-and-continue (nudge injected as the resumed prompt).
        if invocation.balance_on_resume:
            from imp.agents.classify import ResumeState
            from imp.providers.codex_resume import derive_codex_resume_state
            rstate, _ = derive_codex_resume_state(
                invocation.events_path, aid)
            if rstate is ResumeState.AWAITING_USER:
                if invocation.user_input_fn is None:
                    yield {"type": "error", "payload": {
                        "category": "config", "kind": "no_user_input_fn",
                        "message": "AWAITING_USER resume requires user_input_fn"}}
                    return
                # Parity with the other providers: the awaiting_user_input gate
                # carries no question payload. The agent's prose IS the question;
                # codex's last_text is already journaled as its llm_response and
                # rendered in the narrative, so an empty prompt loses nothing.
                elog.append(agent_id=aid, type="awaiting_user_input",
                            payload={"prompt": ""})
                # §2 resume-reply join (F5): queued interjects satisfy the
                # bare-turn-end yield directly. Fall back to user_input_fn only
                # when the queue is empty.
                reply: str = await _resolve_wait_reply(
                    invocation.coordinator, invocation.user_input_fn)
                user_msg = Message(
                    role="user", content=(TextBlock(text=reply),))
                elog.append_user_message(agent_id=aid, message=user_msg)
                next_prompt = reply
            elif rstate is ResumeState.MISSING_COMPLETE_STEP:
                # Replay-and-continue: nudge is the resumed prompt; journal for audit trail.
                from imp.agents.transcript import RESUME_CONTINUE_NUDGE
                nudge_msg = Message(
                    role="user", content=(TextBlock(text=RESUME_CONTINUE_NUDGE),))
                elog.append_user_message(agent_id=aid, message=nudge_msg)
                next_prompt = RESUME_CONTINUE_NUDGE
        # §2 resume-entry drain: fold pending interject texts into the first
        # resumed prompt (codex re-prompts with a string). AWAITING_USER already
        # folded the reply above, so skip it here. Only runs on resume
        # (balance_on_resume=True); fresh-entry has no queued interjects.
        coordinator = invocation.coordinator
        if coordinator is not None and invocation.balance_on_resume:
            from imp.agents.classify import ResumeState as _RS
            if rstate is not _RS.AWAITING_USER:
                parts: list[str] = []
                while True:
                    t = coordinator.drain_interject()
                    if t is None:
                        break
                    parts.append(t)
                    elog.append_user_message(
                        agent_id=aid,
                        message=Message(role="user", content=(TextBlock(text=t),)))
                    elog.append(agent_id=aid, type="user_interjected",
                                payload={"text": t})
                if parts:
                    next_prompt = next_prompt + "\n\n" + "\n".join(parts)
        # AbortController for interrupt() — tolerate a fake/SDK without one.
        try:
            from openai_codex_sdk import (
                AbortController,
            )
            self._abort_controller = AbortController()
        except ImportError:
            self._abort_controller = None

        guard = LoopGuard()

        while True:
            # Reset per-turn completion-capture state before each turn so a
            # stale flag from a missed-completion turn cannot satisfy the
            # check on the next.
            proc_state["complete_step_called"] = False
            proc_state["complete_step_args"] = {}
            proc_state["turn_tool_outcomes"] = []
            # Mirror onto the shared MCP RunState (which the dispatcher writes).
            state.complete_step_called = False
            state.complete_step_args = {}
            streamed = await thread.run_streamed(next_prompt)
            need_nudge = False  # set when turn ends without complete_step
            async for event in streamed.events:
                # Dead-server poll: if the MCP server task has ended, bail
                # immediately rather than letting codex's retry-loop be
                # misread as "missing complete_step".
                if server_handle.task.done():
                    exc = (server_handle.task.exception()
                           if not server_handle.task.cancelled() else None)
                    yield {"type": "error", "payload": {
                        "category": "network", "kind": "mcp_server_died",
                        "message": "MCP server task ended mid-turn",
                        "detail": {"server_error": str(exc) if exc else ""}}}
                    return
                if self._cancel:
                    yield {"type": "error", "payload": {
                        "category": "config", "kind": "interrupted",
                        "message": "runner cancelled by interrupt"}}
                    return
                # Post-API-site analog (Decision 10): one sweep per streamed
                # SDK event. Dead-server/cancel checks stay first.
                await _bg._sweep_bg(ctx)
                etype = getattr(event, "type", None)
                process_event(event, elog=elog, agent_id=aid, state=proc_state,
                              provider=invocation.provider_name,
                              model=self.model, run_state=state,
                              subagent_defs=invocation.subagent_defs)

                # Cap trip from inside the MCP wrapper → abort the SDK + bail.
                if state.abort_requested:
                    if self._abort_controller is not None:
                        with contextlib.suppress(Exception):
                            self._abort_controller.abort()
                    await terminate_running_processes(ctx)
                    yield {"type": "error", "payload": {
                        "category": "config",
                        "kind": "tool_call_ceiling_exceeded",
                        "ceiling": state.cap,
                        "message":
                            f"codex tool dispatch cap ({state.cap}) "
                            f"exceeded"}}
                    return

                if state.fatal_tool_error is not None:
                    if self._abort_controller is not None:
                        with contextlib.suppress(Exception):
                            self._abort_controller.abort()
                    tool_name, msg = state.fatal_tool_error
                    # Same payload shape as claude_code.py fatal_tool_error:
                    # `tool` at the top level, `message` is the raw fatal-tool
                    # message.
                    yield {"type": "error", "payload": {
                        "category": "config", "kind": "fatal_tool_error",
                        "tool": tool_name, "message": msg}}
                    return

                # Drain any bash kills detected by the MCP wrapper (non-terminal).
                # tool_killed is an AUDIT event — persisted once via elog.append,
                # NOT yielded. drive_invocation re-appends every yielded event to
                # the same log, so yielding would cause a double-write.
                while state.killed_tool_uses:
                    info = state.killed_tool_uses.pop(0)
                    elog.append(agent_id=aid, type="tool_killed", payload=info)

                # Native-tool violation → fail-closed. AC #18: unconditional
                # revert + bail.
                if proc_state.get("native_violation") is not None:
                    detail = proc_state["native_violation"]
                    restore_to_snapshot(ctx.project_root, entry_snapshot)
                    yield {"type": "error", "payload": {
                        "category": "config", "kind": "native_tool_violation",
                        "message": (
                            f"codex used a native {detail['kind']} tool; "
                            "worktree reverted to entry state."),
                        "detail": {**detail, "kept_bytes": False}}}
                    return

                # Mirror complete_step_args from the shared state into
                # proc_state so the across-turn check below sees them at the
                # next boundary.
                if state.complete_step_called:
                    proc_state["complete_step_called"] = True
                    proc_state["complete_step_args"] = dict(
                        state.complete_step_args)

                # turn.failed → categorize + bail.
                if etype == "turn.failed":
                    exc = categorize_turn_failed(
                        getattr(event, "error", {}) or {})
                    yield perr.make_error_event(exc)
                    return

                if etype == "turn.completed":
                    record = TurnRecord(
                        assistant_text=proc_state.get("last_agent_text", "") or "",
                        tools=list(proc_state.get("turn_tool_outcomes", [])),
                        has_complete_step=bool(proc_state.get("complete_step_called")),
                    )
                    # Every turn observes the loop guard: repeated no-progress
                    # automated nudges accumulate toward a trip (the C1
                    # loop-guard backstop the unification work was waiting on).
                    loop_err = guard.observe(record)
                    if loop_err is not None:
                        yield perr.make_error_event(
                            loop_err, kind=getattr(loop_err, "kind", ""))
                        return
                    if proc_state.get("complete_step_called"):
                        args = proc_state.get("complete_step_args") or {}
                        try:
                            validate_agent_output(
                                args, invocation.agent_def.output_schema)
                        except Exception as ve:
                            yield {"type": "error", "payload": {
                                "category": "agent_output_missing",
                                "kind": "output_validation",
                                "message": str(ve)}}
                            return
                        yield {"type": "agent_output", "payload": args}
                        return
                    # Not captured: classified provider-error text wins over
                    # both nudge and `no_complete_step`. Auth/quota substrings
                    # must short-circuit nudge attempts (a nudge to a quota'd
                    # account just burns more).
                    last_text = proc_state.get("last_agent_text", "") or ""
                    from imp.providers.claude_code import _classify_agent_text
                    classified = _classify_agent_text(last_text)
                    if classified is not None:
                        yield perr.make_error_event(
                            classified, kind=getattr(classified, "kind", ""))
                        return
                    # Turn ended without complete_step. If a continuation_fn is
                    # wired, cross to the outer loop for the autonomous nudge;
                    # the closure itself decides when to stop
                    # (returning None -> terminate via no_complete_step_after_
                    # nudges). Without a continuation_fn there is no way to
                    # continue, so emit the immediate `no_complete_step`.
                    if invocation.continuation_fn is not None:
                        need_nudge = True
                        break  # exit inner for; outer while handles nudge
                    yield {"type": "error", "payload": {
                        "category": "agent_output_missing",
                        "kind": "no_complete_step",
                        "message": ("codex completed turn without calling "
                                    "complete_step"),
                        "detail": {"last_agent_text": last_text[:500]}}}
                    return
            # Stream ended (possibly silently) after a cancel without the
            # in-loop check firing — classify it as a clean interrupt.
            if self._is_cancelled():
                yield {"type": "error", "payload": {
                    "category": "config", "kind": "interrupted",
                    "message": "runner cancelled by interrupt"}}
                return
            # Inner loop exited. If we set need_nudge, invoke continuation_fn
            # and either run another turn or yield the after-nudges error.
            if not need_nudge:
                return
            last_text = proc_state.get("last_agent_text", "") or ""
            import inspect as _inspect
            cont = invocation.continuation_fn
            assert cont is not None  # guarded by need_nudge condition
            try:
                nxt = cont(last_text)
                if _inspect.isawaitable(nxt):
                    nxt = await nxt
            except AwaitingUserInput:
                raise  # headless bare-turn-end pause unwinds, not classified
            except Exception as exc:
                raise_if_suspended(exc)  # a subagent-origin pause must unwind
                if self._is_cancelled():
                    yield {"type": "error", "payload": {
                        "category": "config", "kind": "interrupted",
                        "message": "runner cancelled by interrupt"}}
                    return
                nxt = None
            # A Ctrl-C that landed while the nudge was computed pauses cleanly
            # instead of re-running a turn.
            if self._is_cancelled():
                yield {"type": "error", "payload": {
                    "category": "config", "kind": "interrupted",
                    "message": "runner cancelled by interrupt"}}
                return
            if nxt is None:
                yield {"type": "error", "payload": {
                    "category": "agent_incomplete",
                    "kind": "no_complete_step_after_nudges",
                    "message": ("codex did not call complete_step before the "
                                "continuation hook stopped nudging"),
                    "detail": {"last_agent_text": last_text[:500]}}}
                return
            elog.append_user_message(
                agent_id=aid,
                message=Message(role="user",
                                content=(TextBlock(text=str(nxt)),)))
            next_prompt = str(nxt)

            # Turn boundary: this is the only point where THIS runner sets up
            # codex's next turn prompt (the codex thread owns the in-turn tool
            # loop). Codex has no message-list transcript and re-prompts with a
            # STRING, so the shared agent_run.deliver_interject helper does not
            # fit — implement the equivalent inline: drain a queued human
            # interject, emit user_interjected + a user_message, and fold the
            # text into next_prompt so codex actually receives it. Reached only
            # after the turn completed (turn.completed), so no subagent is
            # mid-dispatch — S4 holds.
            coordinator = invocation.coordinator
            if coordinator is not None:
                interject_text = coordinator.drain_interject()
                if interject_text is not None:
                    elog.append_user_message(
                        agent_id=aid,
                        message=Message(role="user",
                                        content=(TextBlock(text=interject_text),)))
                    elog.append(agent_id=aid, type="user_interjected",
                                payload={"text": interject_text})
                    next_prompt = f"{next_prompt}\n\n{interject_text}"

    def _build_thread_options(self) -> Any:
        """ThreadOptions with the design's exact production flags. Lives in its
        own method so the test seam can replace it without touching
        `_drive_turns`. Returns None when the SDK is unavailable (test/fake
        path) so `_drive_turns` starts the thread without options.

        `webSearchEnabled` is deliberately NOT set: codex >=0.137 deprecated the
        SDK-emitted `features.web_search_request` flag (it no longer disables and
        emits a noisy `error` item every turn), so web_search is controlled via
        config.toml's top-level `web_search` key instead (see
        `render_config_toml`). web_search is model-hosted, so
        `networkAccessEnabled` stays False regardless (the command sandbox cannot
        reach the network)."""
        try:
            from openai_codex_sdk import (
                ThreadOptions,
            )
        except ImportError:
            return None
        # Use the SDK's declared (alias) parameter names. ThreadOptions is a
        # pydantic model with `populate_by_name=True`, so the snake_case field
        # names work identically at runtime — but only these camelCase aliases
        # match the typed `__init__` signature, so this is what mypy checks
        # against. Verified: both forms model_dump() to the same payload.
        return ThreadOptions(
            model=self.model,
            sandboxMode="danger-full-access",
            approvalPolicy=CODEX_APPROVAL_POLICY,
            networkAccessEnabled=False,
        )

    def _build_prompt(self, system_prompt: str) -> str:
        """Prepend the anti-native-tool instruction (design: soft enforcement
        backstopped by the tripwire)."""
        return (
            system_prompt
            + "\n\n"
            + "IMPORTANT — Tool policy:\n"
            + "- Use ONLY the `mcp__imp__*` tools exposed via the MCP server.\n"
            + "- NEVER call native `apply_patch` or any other built-in tool.\n"
            + "- Finish the task by calling the `complete_step` tool with the\n"
            + "  structured answer.\n"
        )

    async def interrupt(self) -> None:
        """Cancel the run — four effects:
          1. set `self._cancel = True`.
          2. `terminate_running_processes(ctx)`.
          3. `terminate_background_processes(ctx)` (Decision 7: pause kills
             bg processes; persisted tombstones explain it on resume).
          4. SDK `AbortController.abort()`.
        Idempotent."""
        from imp.agents.tools.bg import terminate_background_processes
        self._cancel = True
        try:
            if self._ctx is not None:
                await terminate_running_processes(self._ctx)
                # Decision 7: pause kills background processes too (leak
                # resistance); the persisted tombstones give the resumed agent an
                # explanation. Idempotent vs the run() finally (both fire on pause).
                await terminate_background_processes(self._ctx)
        finally:
            # abort() must fire even if a teardown raises (disk-full tombstone
            # write): it is the only effect that stops a stalled SDK stream.
            #
            # KNOWN LIMITATION (codex grandchild-reaping gap): unlike claude_code,
            # imp does NOT group-reap the `codex exec` child on pause. The codex
            # SDK keeps that child's pid local to CodexExec.run() and never exposes
            # it, so there is no handle to killpg. Session isolation
            # (codex_subprocess_patch) still makes the child its own session leader
            # so a terminal Ctrl-C reaches imp alone; teardown then relies on the
            # SDK's cooperative AbortSignal -> single proc.kill(), which can orphan
            # the child's own grandchildren. Reaching deeper into SDK internals to
            # expose that pid is deliberately out of scope.
            if self._abort_controller is not None:
                with contextlib.suppress(Exception):
                    self._abort_controller.abort()

    def _is_cancelled(self) -> bool:
        """Read the interrupt flag opaquely.

        continuation_fn (and the async transport) can flip ``self._cancel``
        between checks within a single ``_drive_turns`` pass; routing these
        re-checks through a method call keeps mypy from narrowing the attribute
        to a stale ``Literal[False]`` and wrongly flagging the later guards as
        unreachable.
        """
        return self._cancel


# ── Event mapping ─────────────────────────────────────────────────────────────


def _attach_usage_to_last_llm_response(
    elog: Any, *, agent_id: str, usage: dict[str, Any],
) -> None:
    """Patch the most recent `llm_response` payload for `agent_id` with
    `usage`. Required on every `EventLog` implementation — see
    `src/imp/engine/state.py`."""
    elog.update_last_llm_response_usage(agent_id=agent_id, usage=usage)


def _serialize_mcp_result(result: Any) -> str:
    """Render an SDK ``McpToolCallResult`` to text for the tool_result event.

    The SDK shape is ``.content`` (a list of MCP content-block dicts) plus
    ``.structured_content``; a plain ``str``/``None`` (older or duck-typed
    shapes) is passed through. Previously the runner did ``str(result)`` on the
    whole object, which serialized a pydantic repr instead of the tool output.
    """
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    blocks = getattr(result, "content", None)
    if isinstance(blocks, list):
        parts: list[str] = []
        for b in blocks:
            if isinstance(b, dict):
                parts.append(b.get("text") or b.get("data") or json.dumps(b))
            else:
                parts.append(str(b))
        text = "\n".join(p for p in parts if p)
        if text:
            return text
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        return structured if isinstance(structured, str) else json.dumps(structured)
    return str(result)


def reassemble_codex_turn(items: list[Any]) -> dict[str, Any]:
    """Reassemble one codex turn's ``item.completed`` items into a full-turn
    ``llm_response.raw`` (``{"content": [...]}``) carrying text + tool_use
    blocks.  This raw IS emitted live as the turn's single ``llm_response`` at
    ``turn.completed`` (Unit F), and is also consumed by audit reconstruction
    (``build_messages_from_events``) for tool_use/tool_result pairing.
    A web_search item becomes a query-only tool_use (codex exposes no
    results/citations).
    """
    content: list[dict[str, Any]] = []
    for item in items:
        itype = getattr(item, "type", None)
        if itype == "agent_message":
            content.append({"type": "text",
                             "text": getattr(item, "text", "") or ""})
        elif itype == "mcp_tool_call":
            name = (getattr(item, "tool", "")
                    or getattr(item, "name", "") or "")
            block_id = getattr(item, "id", "") or ""
            content.append({
                "type": "tool_use",
                "id": block_id,
                "name": name,
                "input": getattr(item, "arguments", None) or {},
            })
        elif itype == "web_search":
            content.append({
                "type": "tool_use",
                "id": getattr(item, "id", "") or "",
                "name": "web_search",
                "input": {"query": getattr(item, "query", "") or ""},
            })
        # reasoning/command_execution/file_change/error are not part of the
        # assistant message's reconstructable content.
    return {"content": content}


def _parse_codex_message(raw: dict[str, Any]) -> Message:
    """raw → assistant Message for codex.  Post-F1, the live `llm_response.raw`
    (emitted at `turn.completed` via `reassemble_codex_turn`) carries both
    text and tool_use blocks; this parser handles both.  A web_search appears
    as a query-only tool_use block.
    Unknown blocks are dropped (surfaced via `_extract_codex_diagnostics`).

    Lives here (not in replay.py) per the provider-exports-its-parser convention:
    replay.py imports + registers it, runner can reuse it, no replay-import cycle.
    """
    content: list[Any] = []
    for block in raw.get("content", []):
        kind = block.get("type")
        if kind == "text":
            content.append(TextBlock(text=block.get("text", "")))
        elif kind == "tool_use":
            content.append(ToolUseBlock(
                id=block["id"], name=block["name"],
                input=block.get("input", {})))
        # unknown: skipped (see _extract_codex_diagnostics)
    return Message(role="assistant", content=tuple(content))


def _extract_codex_diagnostics(raw: dict[str, Any]) -> Diagnostics:
    """Render-only diagnostics for codex. text/tool_use are conversation
    history (not warnings); any other block type is surfaced as a warning.
    codex's reasoning is emitted as a separate `thinking` event, not in
    `llm_response.raw`, so there are no inline thinkings to extract here."""
    warnings: list[str] = []
    for block in raw.get("content", []):
        kind = block.get("type")
        if kind in ("text", "tool_use"):
            continue
        warnings.append(f"skipped unknown block type {kind!r}")
    return Diagnostics(block_warnings=tuple(warnings), thinkings=())


def process_event(
    event: Any, *, elog: Any, agent_id: str, state: dict[str, Any],
    provider: str, model: str, run_state: Any = None,
    subagent_defs: dict[str, AgentDef] | None = None,
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

    # `item.started` — open the task fold keyed on the SDK item id (Change 2).
    # The codex SDK emits item.started (status="in_progress") for an mcp_tool_call
    # before item.completed, carrying the SAME id; opening the task tool_call here
    # lets out-of-order sibling completion pair by id with no re-key. Non-task
    # item.started carries no final content and is intentionally ignored.
    if etype == "item.started":
        item = getattr(event, "item", None)
        name = getattr(item, "tool", "") or getattr(item, "name", "") or ""
        if getattr(item, "type", None) == "mcp_tool_call" and name == "sub_agent":
            sdk_id = getattr(item, "id", "") or ""
            args = getattr(item, "arguments", None) or {}
            # `arguments` may be a JSON string; mirror the item.completed guard.
            if isinstance(args, str):
                try:
                    parsed = json.loads(args)
                except (ValueError, TypeError):
                    parsed = None
                lookup = parsed if isinstance(parsed, dict) else {}
            elif isinstance(args, dict):
                lookup = args
            else:
                lookup = {}
            defs = subagent_defs or {}
            sn = lookup.get("subagent_name")
            sub_def = defs.get(sn) if isinstance(sn, str) else None
            label = sub_def.label if sub_def is not None else None
            elog.append_tool_call(
                agent_id=agent_id,
                block=ToolUseBlock(id=sdk_id, name="sub_agent", input=args),
                label=label)
        return

    # `item.completed` — dispatch on the item's own type.
    # (A task `item.started` is handled just above — it opens the fold; only its
    # `item.completed` carries the final content that closes it.)
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
            state["last_agent_text"] = text  # still needed by nudge/classify
            # Buffer the item; the full-turn llm_response (text + tool_use) is
            # reassembled and emitted once at turn.completed (Unit F). Emitting
            # a per-message text-only response here would lose the tool_use
            # blocks the audit pairing needs.
            state.setdefault("turn_items", []).append(item)
            return
        if itype == "mcp_tool_call":
            # SDK `McpToolCallItem` exposes `tool` + `server`, NOT `name`; reading
            # `name` silently recorded "" for every call (and made the
            # complete_step capture below dead). Fall back to `name` only for
            # older/duck-typed shapes.
            name = getattr(item, "tool", "") or getattr(item, "name", "") or ""
            args = getattr(item, "arguments", None) or {}
            sdk_id = getattr(item, "id", "") or ""
            # Parse a string-shaped `arguments` to a dict for ToolOutcome.input
            # recording and complete_step fallback capture. Unparseable strings
            # degrade gracefully (no exception).
            match_args: dict[str, Any] | None
            if isinstance(args, str):
                try:
                    parsed = json.loads(args)
                except (ValueError, TypeError):
                    parsed = None
                match_args = parsed if isinstance(parsed, dict) else None
            elif isinstance(args, dict):
                match_args = args
            else:
                match_args = None
            err = getattr(item, "error", None)
            status = getattr(item, "status", "completed")
            is_error = err is not None or status not in ("completed", "succeeded")
            if err is not None:
                content = getattr(err, "message", "") or str(err)
            else:
                content = _serialize_mcp_result(getattr(item, "result", None))
            call_id = sdk_id
            task_id_ns: str | None
            if name == "sub_agent":
                # Change 2: the task tool_call was opened at item.started keyed on
                # this same SDK id; close the fold here with the matching result
                # (id_ns="event" marks the codex namespace for the §3 fold probe).
                # A task item with no prior item.started (the no-pair path) still
                # emits a lone keyed result.
                task_id_ns = "event"
            else:
                # Non-task tool: emit the tool_call/result pair keyed on the SDK id.
                task_id_ns = None
                elog.append_tool_call(
                    agent_id=agent_id,
                    block=ToolUseBlock(id=call_id, name=name, input=args),
                )
            elog.append_tool_result(
                agent_id=agent_id,
                block=ToolResultBlock(
                    tool_use_id=call_id, content=content, is_error=is_error,
                ),
                id_ns=task_id_ns,
            )
            if name == "complete_step":
                # Fallback capture from the RAW SDK item args, which carry no
                # Pydantic None-injection (unlike the in-process MCP wrapper, so
                # no None-filtering is needed here). When the wrapper's
                # RunState capture ran, the consume loop's mirror overwrites
                # proc_state with its already-filtered args; this write is the
                # fallback for when that path didn't run.
                state["complete_step_args"] = match_args if match_args is not None else {}
                state["complete_step_called"] = True
            # Accumulate tool outcome for the per-turn LoopGuard record.
            # codex item tool names are already bare (no mcp__imp__ prefix),
            # so no stripping is needed.
            state.setdefault("turn_tool_outcomes", []).append(
                ToolOutcome(name=name, tool_use_id=call_id,
                            input=match_args if match_args is not None else {},
                            is_error=is_error,
                            result_preview=_result_preview(content)))
            # Buffer for the full-turn llm_response reassembly (Unit F).
            state.setdefault("turn_items", []).append(item)
            return
        if itype == "web_search":
            query = getattr(item, "query", "") or ""
            if state.get("web_search_enabled"):
                # Expected: the agent declared `web_search` and the SDK ran the
                # model-hosted search. Surface it as a tool_call so it shows in
                # the narrative/`imp log`. codex's WebSearchItem exposes only the
                # query (no results/citations), so the result is informational.
                call_id = getattr(item, "id", "") or ""
                elog.append_tool_call(
                    agent_id=agent_id,
                    block=ToolUseBlock(
                        id=call_id, name="web_search",
                        input={"query": query}),
                )
                elog.append_tool_result(
                    agent_id=agent_id,
                    block=ToolResultBlock(
                        tool_use_id=call_id, content="", is_error=False),
                )
                # Buffer for the full-turn llm_response reassembly (Unit F);
                # the reassembler maps it to a query-only tool_use block.
                state.setdefault("turn_items", []).append(item)
                return
            # Disabled (`webSearchEnabled=False`): the tool should be gone.
            # Seeing one is a soft tripwire (not fail-closed by itself; the
            # SDK-form disable is verified live in the e2e test).
            elog.append(agent_id=agent_id, type="runner_warning",
                        payload={"message":
                                 f"unexpected codex web_search item "
                                 f"(query={query!r})"})
            return
        if itype in ("file_change", "command_execution"):
            # Native-tool violation: recording it here triggers the runner's
            # unconditional revert at the next turn boundary (hard run failure).
            if itype == "file_change":
                # SDK `FileChangeItem` carries `changes: [{path, kind}, …]`, not a
                # top-level `path`; reading `path` yielded "?" for real edits.
                changes = getattr(item, "changes", None) or []
                paths = []
                for c in changes:
                    p = c.get("path") if isinstance(c, dict) else getattr(c, "path", None)
                    if p:
                        paths.append(p)
                detail = ", ".join(paths) or getattr(item, "path", None) or "?"
            else:
                detail = getattr(item, "command", None) or "?"
            state["native_violation"] = {
                "kind": itype, "detail": detail,
            }
            elog.append(agent_id=agent_id, type="runner_warning",
                        payload={
                            "message": f"native {itype} detected: {detail!r}"})
            return
        if itype == "error":
            # codex surfaces some non-fatal conditions as `error` items (e.g. the
            # config-deprecation notice emitted every turn on codex >=0.137).
            # Record the real message as a diagnostic instead of the misleading
            # "unhandled codex item type" warning. Hard turn failures arrive
            # separately as `turn.failed` events, handled below.
            msg = getattr(item, "message", "") or ""
            elog.append(agent_id=agent_id, type="runner_warning",
                        payload={"message": f"codex error item: {msg}",
                                 "codex_error_item": True})
            return
        # Other item types mapped in later tasks; fall through to default arm.
        elog.append(agent_id=agent_id, type="runner_warning",
                    payload={"message": f"unhandled codex item type: {itype!r}"})
        return

    if etype == "turn.completed":
        # Flush the turn's buffered items into ONE full-turn llm_response
        # (text + tool_use) BEFORE usage attaches, so the usage patch below
        # lands on this reassembled response (Unit F). Emitting it here — not
        # per agent_message — gives the audit reconstruction a single response
        # carrying the tool_use blocks paired with their tool_results.
        items = state.pop("turn_items", [])
        if items:
            raw = reassemble_codex_turn(items)
            elog.append_llm_response(
                agent_id=agent_id, provider=provider, model=model,
                raw=raw, usage={}, stop_reason=None,
                auth_mode=state.get("auth_mode"),
            )
        usage = getattr(event, "usage", None)
        if usage is None:
            return
        # Map flat codex keys explicitly. The existing `_normalize_usage`
        # OpenAI-shape detector reads `input_tokens_details.cached_tokens`
        # (nested) — codex emits a FLAT `cached_input_tokens` instead, so
        # without this mapping cache would silently record as zero.
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        cached = int(getattr(usage, "cached_input_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        reasoning_tokens = int(getattr(usage, "reasoning_output_tokens", 0) or 0)
        normalized = {
            # `input_tokens` here means FRESH input (anthropic-style) so the
            # downstream `_normalize_usage` anthropic branch picks it up
            # correctly: fresh = anthropic input, cache_read = cache.
            "input_tokens": max(0, input_tokens - cached),
            "output_tokens": output_tokens,
            "cache_read_input_tokens": cached,
            "cache_creation_input_tokens": 0,
            "reasoning_output_tokens": reasoning_tokens,
        }
        # Attach to the most recent llm_response payload for this agent_id.
        _attach_usage_to_last_llm_response(elog, agent_id=agent_id,
                                           usage=normalized)
        return

    # Default arm: turn.failed / error / unknown —
    # handled by subsequent tasks. Falling through here is benign.


# ── Error categorization ──────────────────────────────────────────────────────


def categorize_turn_failed(error: dict[str, Any] | Any) -> perr.ProviderError:
    """Map a codex ``turn.failed.error`` body into an imp ``ProviderError`` subclass.

    Known cases:
      - HTTP 401 / "Unauthorized" → ``AuthError``
      - HTTP 400 "model … not supported … ChatGPT account" → ``ConfigError``
      - subprocess network/timeout → ``NetworkError``
      - any other → ``ApiError`` (terminal; maps to Pause).
    """
    if not isinstance(error, dict):
        return perr.ApiError(str(error))
    status = error.get("status")
    message = str(error.get("message", "") or "")
    kind = str(error.get("kind", "") or "")

    if status == 401 or "unauthorized" in message.lower():
        return perr.AuthError(f"codex: {message[:200]}")
    if status == 400 and "chatgpt account" in message.lower():
        return perr.ConfigError(f"codex: {message[:300]}")
    if kind == "network" or "timeout" in message.lower() or "network" in message.lower():
        return perr.NetworkError(f"codex: {message[:200]}")
    return perr.ApiError(f"codex: {message[:200]}")


# ── Imp-side MCP server-start ping ─────────────────────────────────────────────


async def _ping_mcp_server(
    *, url: str, bearer_token: str, timeout_seconds: float,
) -> None:
    """Imp-side server-start ping (spec run() flow step 2).

    Sends a single bearer-auth'd POST to the MCP endpoint and treats any HTTP
    response (even a 4xx body — initialize handshake details vary by mcp
    version) as evidence the server is reachable. A 401 here would mean our
    bearer-token gate is mis-wired; anything else (200/202/422/501) proves
    reachability. Connection refused / DNS / timeout → raise so `run()`
    fails fast with `network` ProviderError.

    Bounded retry: up to `timeout_seconds` wall total, in 100 ms slices, so a
    slow uvicorn startup (cold-import cost of starlette + mcp) doesn't
    false-negative the ping.
    """
    import asyncio

    import httpx
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    last_exc: Exception | None = None
    while asyncio.get_running_loop().time() < deadline:
        try:
            async with httpx.AsyncClient(timeout=0.5) as client:
                r = await client.post(
                    url,
                    json={"jsonrpc": "2.0", "id": 0,
                          "method": "initialize", "params": {}},
                    headers={"Authorization": f"Bearer {bearer_token}"},
                )
                if r.status_code == 401:
                    raise RuntimeError(
                        "bearer-token gate rejected imp's own ping — "
                        "middleware is mis-wired")
                return
        except (httpx.ConnectError, httpx.ReadError, httpx.ConnectTimeout,
                httpx.ReadTimeout) as exc:
            last_exc = exc
            await asyncio.sleep(0.1)
        except Exception:
            raise
    raise RuntimeError(
        f"MCP endpoint not reachable within {timeout_seconds}s "
        f"({last_exc!r})")


# ── Dead-server race helper ───────────────────────────────────────────────────


@dataclass(frozen=True)
class RaceOutcome:
    kind: str  # "consume_done" | "server_died" | "timeout"
    detail: str = ""


async def race_consume_against_server(
    consume_task: asyncio.Task[Any], serve_task: asyncio.Task[Any],
    *, timeout: float,
) -> RaceOutcome:
    """Race two tasks; classify which finished first.

    Per design "Distinguish a dead MCP server from a retried tool error":
      - `serve_task` completes first ⇒ server died ⇒ classify api/network and
        bail (don't let codex retry-loop look like "missing complete_step").
      - `consume_task` completes first ⇒ stream finished normally.
      - Neither completes within `timeout` ⇒ timeout (caller decides).
    """
    done, _pending = await asyncio.wait(
        {consume_task, serve_task},
        return_when=asyncio.FIRST_COMPLETED,
        timeout=timeout,
    )
    if not done:
        return RaceOutcome(kind="timeout")
    if serve_task in done:
        exc = serve_task.exception() if not serve_task.cancelled() else None
        return RaceOutcome(
            kind="server_died",
            detail=str(exc) if exc is not None else "serve_task ended unexpectedly")
    return RaceOutcome(kind="consume_done")
