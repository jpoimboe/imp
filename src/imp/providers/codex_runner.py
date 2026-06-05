"""CodexAgentRunner — the heavy lifting for the `codex` provider.

Implements: interrupt() with three effects, fail-fast preflight(),
snapshot/restore mechanism (git stash create + untracked set), and
process_event() for basic event types. The full run() is added in later tasks.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import shutil
import subprocess
import tempfile
from collections.abc import AsyncGenerator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from imp.agents.runner import AgentInvocation, Event
from imp.agents.tools.context import ToolContext, terminate_running_processes
from imp.engine.schema import validate_agent_output
from imp.engine.state import JsonlEventLog
from imp.providers import errors as perr
from imp.providers.codex_config import (
    AuthMode,
    detect_auth_mode,
    provision_codex_home,
    seed_auth,
    sweep_stale_codex_homes,
    teardown_codex_home,
)
from imp.providers.codex_mcp import RunState, start_server

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

    async def run(self, invocation: AgentInvocation) -> AsyncGenerator[Event, None]:
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
        except Exception as exc:
            yield perr.make_unknown_error_event(exc)
            return

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
            fs_permissions=invocation.fs_permissions,
            events_path=invocation.events_path,
            subagent_defs=invocation.subagent_defs,
            provider_name=invocation.provider_name,
            model_name=invocation.model_name,
            parent_transcript_dir=invocation.transcript_dir,
            interactive=invocation.interactive,
        )
        ctx.change_context = invocation.change_context
        ctx.change_state = invocation.change_state
        ctx.spawn_subagent = spawn_subagent_via_registry
        ctx.resolved_tiers = invocation.resolved_tiers
        ctx.subagent_depth = invocation.subagent_depth
        ctx.coordinator = invocation.coordinator
        ctx.tiers = invocation.tiers
        ctx.lint_command = invocation.lint_command
        ctx.test_command = invocation.test_command
        ctx.non_interactive = invocation.non_interactive
        ctx.event_log = elog
        if self._user_input_fn is not None:
            ctx.user_input_fn = self._user_input_fn
        self._ctx = ctx

        state = RunState(cap=invocation.max_tool_calls or 1000)
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

        # 5a. Imp-side server-start ping: a single bearer-auth'd HTTP probe
        # against `server_handle.url` to fail fast before provisioning if imp
        # can't reach its own server.
        try:
            await _ping_mcp_server(
                url=server_handle.url, bearer_token=bearer_token,
                timeout_seconds=2.0,
            )
        except Exception as ping_exc:
            await server_handle.shutdown()
            yield perr.make_error_event(perr.NetworkError(
                (
                    f"imp-side MCP server-start ping failed; codex would not "
                    f"be able to reach the MCP endpoint either. "
                    f"({type(ping_exc).__name__}: {ping_exc})"),
                detail={"url": server_handle.url},
            ))
            return

        # 6. Provision CODEX_HOME + config.toml + seed auth.
        codex_home = provision_codex_home(
            tmp_root=tmp_root, host=server_handle.host,
            port=server_handle.port, bearer_env_var="IMP_MCP_TOKEN",
        )
        seed_auth(mode=auth_mode, canonical_path=canonical_auth,
                  codex_home=codex_home, env=os.environ)

        # 7. Run the SDK turn(s) and consume events.
        try:
            client = self._build_client(codex_home=codex_home,
                                        bearer_token=bearer_token)
            async for ev in self._drive_turns(
                client=client, invocation=invocation, elog=elog,
                aid=aid, ctx=ctx, state=state, proc_state=proc_state,
                entry_snapshot=entry_snapshot,
            ):
                yield ev
        finally:
            # 8. Lifecycle teardown — runs on EVERY exit (success, error,
            #    abort, interrupt). (Task 31 hardens this further.)
            await server_handle.shutdown()
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

    def _build_client(self, *, codex_home: Path, bearer_token: str) -> Any:
        """Construct the SDK `Codex` client. Tests inject a `client_factory`
        callable that takes `options=` and returns a FakeCodex."""
        if self._client_factory is not None:
            return self._client_factory(options=None)
        from openai_codex_sdk import (
            Codex,
            CodexOptions,
        )

        from imp.engine.env import overlay_engine_env
        env = overlay_engine_env(os.environ, {
            "CODEX_HOME": str(codex_home),
            "IMP_MCP_TOKEN": bearer_token,
        })
        return Codex(CodexOptions(env=env))

    async def _drive_turns(
        self, *, client: Any, invocation: AgentInvocation, elog: Any,
        aid: str, ctx: ToolContext, state: RunState, proc_state: dict[str, Any],
        entry_snapshot: EntrySnapshot,
    ) -> AsyncGenerator[Event, None]:
        """Start a thread, run streamed turns, consume events.

        Multi-turn nudge loop (Task 28): on a turn that completes without
        `complete_step`, classified provider-error text short-circuits; an
        interactive run with a wired `continuation_fn` is nudged for another
        turn; autonomous or exhausted runs emit the appropriate terminal
        error. Inline cap-abort / native-violation / turn.failed / interrupt
        handling is preserved verbatim inside the inner loop.
        """
        thread_options = self._build_thread_options()
        if thread_options is None:
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
        # Whole-run nudge budget (across-turn). `continuation_fn` is the gate;
        # this cap is the floor.
        max_nudges = 3
        nudges_used = 0
        # AbortController for interrupt() — tolerate a fake/SDK without one.
        try:
            from openai_codex_sdk import (
                AbortController,
            )
            self._abort_controller = AbortController()
        except ImportError:
            self._abort_controller = None

        while True:
            # Reset per-turn completion-capture state before each turn so a
            # stale flag from a missed-completion turn cannot satisfy the
            # check on the next.
            proc_state["complete_step_called"] = False
            proc_state["complete_step_args"] = {}
            # Mirror onto the shared MCP RunState (which the dispatcher writes).
            state.complete_step_called = False
            state.complete_step_args = {}
            streamed = await thread.run_streamed(next_prompt)
            need_nudge = False  # set when turn ends without complete_step
            async for event in streamed.events:
                if self._cancel:
                    yield {"type": "error", "payload": {
                        "category": "config", "kind": "interrupted",
                        "message": "runner cancelled by interrupt"}}
                    return
                etype = getattr(event, "type", None)
                process_event(event, elog=elog, agent_id=aid, state=proc_state,
                              provider=invocation.provider_name,
                              model=self.model)

                # Cap trip from inside the MCP wrapper → abort the SDK + bail.
                if state.abort_requested:
                    if self._abort_controller is not None:
                        with contextlib.suppress(Exception):
                            self._abort_controller.abort()
                    await terminate_running_processes(ctx, timeout=2.0)
                    yield {"type": "error", "payload": {
                        "category": "config",
                        "kind": "tool_call_ceiling_exceeded",
                        "ceiling": state.cap,
                        "message":
                            f"codex tool dispatch cap ({state.cap}) "
                            f"exceeded"}}
                    return

                # Native-tool violation → fail-closed (autonomous): revert.
                if proc_state.get("native_violation") is not None:
                    restore_to_snapshot(ctx.project_root, entry_snapshot)
                    detail = proc_state["native_violation"]
                    yield {"type": "error", "payload": {
                        "category": "config", "kind": "native_tool_violation",
                        "message": f"codex used a native {detail['kind']} "
                                   f"tool; worktree reverted to entry state.",
                        "detail": detail}}
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
                    # Interactive + continuation_fn → cross to outer loop.
                    if (invocation.interactive
                            and invocation.continuation_fn is not None
                            and nudges_used < max_nudges):
                        need_nudge = True
                        break  # exit inner for; outer while handles nudge
                    # Autonomous OR no continuation_fn OR nudges exhausted:
                    # autonomous emits `no_complete_step`; interactive with a
                    # wired continuation_fn that exhausted the budget emits the
                    # distinct `no_complete_step_after_nudges`.
                    if (invocation.interactive
                            and invocation.continuation_fn is not None
                            and nudges_used >= max_nudges):
                        kind = "no_complete_step_after_nudges"
                        category = "agent_incomplete"
                        msg = ("codex did not call complete_step after "
                               f"{max_nudges} nudges")
                    else:
                        kind = "no_complete_step"
                        category = "agent_output_missing"
                        msg = ("codex completed turn without calling "
                               "complete_step")
                    yield {"type": "error", "payload": {
                        "category": category, "kind": kind, "message": msg,
                        "detail": {"last_agent_text": last_text[:500]}}}
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
            except Exception:
                nxt = None
            if nxt is None:
                yield {"type": "error", "payload": {
                    "category": "agent_incomplete",
                    "kind": "no_complete_step_after_nudges",
                    "message": ("codex did not call complete_step "
                                f"after {nudges_used + 1} nudge(s)"),
                    "detail": {"last_agent_text": last_text[:500]}}}
                return
            from imp.agents.runner import Message, TextBlock
            elog.append_user_message(
                agent_id=aid,
                message=Message(role="user",
                                content=(TextBlock(text=str(nxt)),)))
            next_prompt = str(nxt)
            nudges_used += 1

    def _build_thread_options(self) -> Any:
        """ThreadOptions with the design's exact production flags. Lives in its
        own method so the test seam can replace it without touching
        `_drive_turns`. Returns None when the SDK is unavailable (test/fake
        path) so `_drive_turns` starts the thread without options."""
        try:
            from openai_codex_sdk import (
                ThreadOptions,
            )
        except ImportError:
            return None
        return ThreadOptions(
            model=self.model,
            sandbox_mode="danger-full-access",
            approval_policy="never",
            web_search_enabled=False,
            network_access_enabled=False,
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


def _attach_usage_to_last_llm_response(
    elog: Any, *, agent_id: str, usage: dict[str, Any],
) -> None:
    """Patch the most recent `llm_response` payload for `agent_id` with
    `usage`. Required on every `EventLog` implementation — see
    `src/imp/engine/state.py`."""
    elog.update_last_llm_response_usage(agent_id=agent_id, usage=usage)


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
            state["last_agent_text"] = text
            elog.append_llm_response(
                agent_id=agent_id, provider=provider, model=model,
                raw={"content": [{"type": "text", "text": text}]},
                usage={}, stop_reason=None,
                auth_mode=state.get("auth_mode"),
            )
            return
        if itype == "mcp_tool_call":
            from imp.agents.runner import ToolResultBlock, ToolUseBlock
            name = getattr(item, "name", "") or ""
            args = getattr(item, "arguments", None) or {}
            call_id = getattr(item, "id", "") or ""
            elog.append_tool_call(
                agent_id=agent_id,
                block=ToolUseBlock(id=call_id, name=name, input=args),
            )
            result = getattr(item, "result", None)
            content = result if isinstance(result, str) else (
                "" if result is None else str(result)
            )
            status = getattr(item, "status", "completed")
            is_error = status not in ("completed", "succeeded")
            elog.append_tool_result(
                agent_id=agent_id,
                block=ToolResultBlock(
                    tool_use_id=call_id, content=content, is_error=is_error,
                ),
            )
            if name == "complete_step":
                # Secondary capture path (idempotent — in-process wrapper sets it too).
                state["complete_step_args"] = dict(args)
                state["complete_step_called"] = True
            return
        if itype == "web_search":
            # `web_search_enabled=False` should remove the tool entirely.
            # Seeing one is a soft tripwire (not fail-closed by itself; the
            # SDK-form disable is verified live in the e2e test).
            elog.append(agent_id=agent_id, type="runner_warning",
                        payload={"message":
                                 f"unexpected codex web_search item "
                                 f"(query={getattr(item, 'query', '')!r})"})
            return
        if itype in ("file_change", "command_execution"):
            # Fail-closed tripwire (design HIGH-3). The native edit ALREADY
            # happened — recording it here triggers the runner's revert path
            # at the next turn boundary. Autonomous: hard run failure.
            # Interactive: user keep-or-revert prompt; default revert.
            detail = (getattr(item, "path", None) or
                      getattr(item, "command", None) or "?")
            state["native_violation"] = {
                "kind": itype, "detail": detail,
            }
            elog.append(agent_id=agent_id, type="runner_warning",
                        payload={
                            "message": f"native {itype} detected: {detail!r}"})
            return
        # Other item types mapped in later tasks; fall through to default arm.
        elog.append(agent_id=agent_id, type="runner_warning",
                    payload={"message": f"unhandled codex item type: {itype!r}"})
        return

    if etype == "turn.completed":
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
    deadline = asyncio.get_event_loop().time() + timeout_seconds
    last_exc: Exception | None = None
    while asyncio.get_event_loop().time() < deadline:
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
