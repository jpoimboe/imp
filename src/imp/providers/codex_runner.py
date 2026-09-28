# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""Codex provider with run-scoped process ownership, preflight, and event mapping."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import secrets
import time
from collections.abc import AsyncGenerator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import anyio

from imp.agents.agent_loop import run_agent_loop
from imp.agents.loop_guard import LoopGuard, ToolOutcome, TurnRecord, _result_preview
from imp.agents.loop_items import Error
from imp.agents.runner import (
    AgentInvocation,
    Event,
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from imp.agents.subagents import make_task_tool, raise_if_suspended
from imp.agents.tools import Tool, get_registry
from imp.agents.tools import bg as _bg
from imp.agents.tools.complete_step import make_complete_step_tool
from imp.agents.tools.context import (
    ToolContext,
    request_bracketed,
    terminate_tool_processes,
)
from imp.agents.tools.load_skill import make_load_skill_tool
from imp.agents.transcript import deliver_continuation_reply
from imp.engine.agent_run import _resolve_wait_reply, continuation_nudge
from imp.engine.awaiting_user_input import AwaitingUserInput
from imp.engine.env import quest_scratch_root
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
from imp.providers.codex_exec import (
    Codex,
    CodexOptions,
    CodexProcessOwner,
    ThreadOptions,
)
from imp.providers.codex_mcp import (
    CODEX_APPROVAL_POLICY,
    RunState,
    TaskStartCoordinator,
    start_impty_server,
    start_server,
)
from imp.providers.codex_rollout import (
    CorrelatedReadings,
    RolloutCursor,
    RolloutReadings,
    RolloutTelemetry,
    RolloutUsage,
    compaction_seen,
)
from imp.providers.codex_token_refresh import (
    FAILED_INVALID_GRANT,
    SKIPPED_ROTATED,
    RefreshOutcome,
    canonical_expires_at,
    refresh_canonical_token,
)
from imp.providers.observe_classify import (
    _classify_agent_text,
    classifies_as_provider_error,
    matches_error_prose,
)
from imp.providers.observe_session import ObserveSession
from imp.providers.stderr_sink import current_stderr_sink
from imp.providers.web_native import WEB_TOOL_NAMES, check_web_capability

_logger = logging.getLogger(__name__)

# ── PreflightError ────────────────────────────────────────────────────────────


class PreflightError(Exception):
    """Raised before thread options are built when the run cannot proceed safely."""
    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category


def preflight(
    *, model: str, canonical_auth: Path, env: Mapping[str, str],
) -> AuthMode:
    """Run fail-fast checks before building thread options. Returns auth mode."""
    # 1. Refuse `*-codex` in BOTH auth modes (M1).
    if model.endswith("-codex"):
        raise PreflightError(
            "config",
            f"model {model!r} is a *-codex variant; codex provider refuses "
            "*-codex in both auth modes until it gets fail-closed obedience "
            "validation. Choose a non-codex model (e.g. gpt-5.5).",
        )
    # 2. Auth must exist.
    mode = detect_auth_mode(canonical_path=canonical_auth, env=env)
    if mode is None:
        raise PreflightError(
            "auth",
            "no usable codex auth (neither ~/.codex/auth.json nor "
            "OPENAI_API_KEY); run `codex login` or export an API key.",
        )
    return mode


# ── CodexAgentRunner ──────────────────────────────────────────────────────────


def _build_tools(invocation: AgentInvocation) -> dict[str, Tool]:
    """Same filtering as anthropic._build_tools, including the bash ->
    bash_output/bash_kill injection."""
    reg = get_registry()
    out: dict[str, Tool] = {}
    check_web_capability(invocation.provider_name, invocation.agent_def)
    for name in invocation.agent_def.tools:
        if name == "complete_step":
            continue
        if name == "sub_agent":
            continue
        if name in WEB_TOOL_NAMES:
            # Reserved native-capability names (§4): never registry-looked-up;
            # routed to the native-injection path at provision time.
            continue
        if name == "load_skill":
            # Auto-injected below; tolerate a redundant explicit declaration.
            continue
        if name not in reg:
            raise KeyError(f"agent declares unknown tool {name!r}")
        out[name] = reg[name]
    if "sub_agent" in invocation.agent_def.tools:
        out["sub_agent"] = make_task_tool(invocation.tiers)
    if invocation.subagent_depth == 0:
        out["complete_step"] = make_complete_step_tool(
            invocation.agent_def.output_schema)
    out["load_skill"] = make_load_skill_tool()
    if "bash" in invocation.agent_def.tools:
        # Auto-granted with bash (same block as anthropic/openai
        # _build_tools): run_in_background is a bash PARAMETER, so an agent
        # with bash but no read/kill tool is a trap — a backgrounded process
        # it can never see or stop. The capability set must be atomic.
        out["bash_output"] = reg["bash_output"]
        out["bash_kill"] = reg["bash_kill"]
    return out


_DEAD_GRANT_MESSAGE = (
    "codex: ChatGPT OAuth refresh token is dead; run `codex login`. Sharing "
    "~/.codex/auth.json across machines kills it — each refresh rotates a "
    "single-use token.")

_APIKEY_FALLBACK_MESSAGE = (
    "codex: ChatGPT credential is dead; continuing with OPENAI_API_KEY — this "
    "bills the OpenAI API, not your ChatGPT subscription")


def _journal_token_refresh(elog: Any, aid: str, outcome: RefreshOutcome) -> None:
    payload: dict[str, Any] = {"outcome": outcome.outcome}
    if outcome.expires_at is not None:
        payload["expires_at"] = outcome.expires_at
    elog.append(agent_id=aid, type="token_refresh", payload=payload)


def _pre_spawn_token_refresh(elog: Any, aid: str) -> RefreshOutcome:
    """Refresh the canonical codex token before seed_auth and journal the
    outcome; the caller owns the dead-token policy."""
    outcome = refresh_canonical_token(int(time.time() * 1000))
    _journal_token_refresh(elog, aid, outcome)
    return outcome


# Internal sentinels _receive yields for the two mid-turn terminal pre-checks
# (dead MCP server, hard cancel); _message_to_items maps each to a terminal Error.
_SERVER_DIED = object()
_CANCEL = object()
_ROLLOUT_WAIT_SECONDS = 2.0
_ROLLOUT_POLL_SECONDS = 0.025


def _rollout_usage(raw: Any) -> RolloutUsage:
    return RolloutUsage(
        input_tokens=int(getattr(raw, "input_tokens", 0) or 0),
        cached_input_tokens=int(getattr(raw, "cached_input_tokens", 0) or 0),
        cache_write_input_tokens=int(getattr(raw, "cache_write_input_tokens", 0) or 0),
        output_tokens=int(getattr(raw, "output_tokens", 0) or 0),
        reasoning_output_tokens=int(getattr(raw, "reasoning_output_tokens", 0) or 0),
    )


async def _wait_for_current_readings(
    telemetry: RolloutTelemetry, cursor: RolloutCursor, usage: RolloutUsage,
) -> RolloutReadings:
    deadline = time.monotonic() + _ROLLOUT_WAIT_SECONDS
    matched = False
    first_scan = True
    while first_scan or time.monotonic() < deadline:
        first_scan = False
        try:
            correlated: CorrelatedReadings = telemetry.scan_current(cursor, usage)
        except OSError:
            correlated = CorrelatedReadings(
                matched=False, readings=RolloutReadings(context=None, quota=None))
        matched = matched or correlated.matched
        if correlated.readings.context is not None:
            return correlated.readings
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        await asyncio.sleep(min(_ROLLOUT_POLL_SECONDS, remaining))
    telemetry.finish_turn(cursor, usage, matched)
    return RolloutReadings(context=None, quota=None)


class CodexSession(ObserveSession):
    """The observe `Session` codex's `run()` drives via `run_agent_loop`.

    Each hook body is a relocated region of the old `_run_loop`/`_drive_turns`.
    `_enter` builds ctx/tools/server/client/state and the fresh-vs-resume prompt
    disposition; `_receive` owns the per-turn `run_streamed` + streamed-event
    scaffold with the dead-server/cancel/sweep pre-checks; `_message_to_items`
    shapes the per-event appends (via `process_event`) and the mid-stream terminal
    checks; `_is_terminal` owns the `turn.completed` handoff/threshold decision.
    The nudge/continuation/park terminal outcome is owned by the shared driver.
    Teardown handles are stashed on the runner so run()'s finally reaches them.
    """

    @property
    def _complete_step_called(self) -> bool:
        return bool(self._proc_state.get("complete_step_called"))

    @property
    def _complete_step_args(self) -> dict[str, Any]:
        return self._proc_state.get("complete_step_args") or {}

    @property
    def _save_result_logged(self) -> bool:
        return bool(self._proc_state.get("save_and_exit_logged"))

    async def _enter(self) -> None:
        invocation = self.invocation
        runner = self.runner
        elog = invocation.event_log or JsonlEventLog(invocation.events_path)
        aid = invocation.agent_id or "step:unnamed"
        self.elog = elog
        self.aid = aid
        self.subagent_depth = invocation.subagent_depth

        # 1. Preflight (auth / *-codex) — raises PreflightError.
        canonical_auth = runner._canonical_auth_path()
        cwd = invocation.quest_context.project_root
        auth_mode = preflight(
            model=runner.model, canonical_auth=canonical_auth, env=os.environ,
        )
        runner._auth_mode = auth_mode
        runner._canonical_auth = canonical_auth

        # 2. Build tools + ToolContext (mirror the claude runner exactly).
        from imp.providers.resolve import spawn_subagent_via_registry
        tools = _build_tools(invocation)
        self.tools = tools
        ctx = ToolContext(
            project_root=cwd,
            events_path=invocation.events_path,
            subagent_defs=invocation.subagent_defs,
            provider_name=invocation.provider_name,
            model_name=invocation.model_name,
            parent_transcript_dir=invocation.transcript_dir,
            agent_id=aid,
            background_enabled=True,  # full bg surface wired: injection + sweeps + teardown
            quest_home=invocation.quest_context.quest_dir,
        )
        ctx.quest_context = invocation.quest_context
        ctx.quest_state = invocation.quest_state
        ctx.quest = invocation.quest          # live quest for rename_quest
        ctx.spawn_subagent = spawn_subagent_via_registry
        ctx.resolved_tiers = invocation.resolved_tiers
        ctx.subagent_depth = invocation.subagent_depth
        ctx.coordinator = invocation.coordinator
        ctx.tiers = invocation.tiers
        ctx.test_gates = invocation.test_gates
        ctx.pythonpath = invocation.pythonpath
        ctx.undecided = invocation.undecided
        ctx.non_interactive = invocation.headless
        ctx.base_sandbox_policy = invocation.sandbox_policy
        ctx.event_log = elog
        if runner._user_input_fn is not None:
            ctx.user_input_fn = runner._user_input_fn
        self.ctx = ctx
        runner._ctx = ctx

        state = RunState(cap=invocation.max_tool_calls or 1000, agent_id=aid)
        state.runner_loop = asyncio.get_running_loop()
        self._state = state
        # Shared event-processing state (process_event keys).
        proc_state: dict[str, Any] = {
            "thread_id": None, "complete_step_args": {},
            "complete_step_called": False, "save_and_exit_logged": False, "auth_mode": auth_mode,
            "context_reading": None, "context_canary_emitted": False,
            "quota_reading": None,
            "turn_output_tokens": None,
            "compaction_canary_emitted": False,
            "turn_tool_outcomes": [], "last_agent_text": "",
        }
        self._proc_state = proc_state
        runner._proc_state = proc_state

        # 3. Stale-CODEX_HOME sweep (best-effort).
        tmp_root = quest_scratch_root()
        sweep_stale_codex_homes(tmp_root=tmp_root, max_age_seconds=24 * 3600)

        # 4. Start the in-process MCP server on an ephemeral port.
        bearer_token = secrets.token_urlsafe(24)
        server_handle = await start_server(
            tools=tools, ctx=ctx, state=state,
            bearer_token=bearer_token, host="127.0.0.1",
        )
        runner._server_handle = server_handle

        from imp.providers.stderr_sink import build_stderr_sink
        runner._stderr_sink = build_stderr_sink(
            invocation.transcript_dir / "stderr.log",
            elog=elog, aid=aid, source="codex")
        runner._stderr_token = current_stderr_sink.set(runner._stderr_sink)

        # 4a. Imp-side server-start ping: a single bearer-auth'd HTTP probe against
        # `server_handle.url` to fail fast before provisioning if imp can't reach
        # its own server. On failure stash the terminal error (events() yields it
        # on first pull); run()'s finally shuts the server down (codex_home is
        # still None, so teardown_codex_home is correctly skipped).
        try:
            await _ping_mcp_server(
                url=server_handle.url, bearer_token=bearer_token,
                timeout_seconds=2.0,
            )
        except Exception as ping_exc:
            self._pending_terminal = Error(payload=cast(dict[str, Any], perr.make_error_event(
                perr.NetworkError(
                    (
                        f"imp-side MCP server-start ping failed; codex would "
                        f"not be able to reach the MCP endpoint either. "
                        f"({type(ping_exc).__name__}: {ping_exc})"),
                    detail={"url": server_handle.url},
                ))["payload"]))
            return

        # 5. Provision CODEX_HOME + config.toml + seed auth. web_search is
        #    controlled via config.toml's top-level key (the deprecated CLI flag no
        #    longer disables it on codex >=0.137), so it must be decided here, at
        #    provision time, from the agent's declared tools.
        impty_bearer: str | None = None
        if "impty" in invocation.agent_def.drivers:
            from imp.agents.tools.context import attach_impty_driver
            impty_bearer = secrets.token_urlsafe(24)
            runner._impty_driver = attach_impty_driver(
                ctx, journal_dir=invocation.transcript_dir)
            runner._impty_handle = await start_impty_server(
                driver=runner._impty_driver, bearer_token=impty_bearer,
                host="127.0.0.1", runner_loop=state.runner_loop,
            )
        from imp.providers.web_native import declared_web_tools
        web_enabled = "web_search" in declared_web_tools(invocation.agent_def)
        codex_home = provision_codex_home(
            tmp_root=tmp_root, host=server_handle.host,
            port=server_handle.port, bearer_env_var="IMP_MCP_TOKEN",
            web_search_enabled=web_enabled,
            impty_url=(runner._impty_handle.url
                       if runner._impty_handle is not None else None),
            impty_bearer_env_var=(
                "IMP_IMPTY_TOKEN" if runner._impty_handle is not None else None),
        )
        runner._codex_home = codex_home
        proc_state["rollout_telemetry"] = RolloutTelemetry(codex_home, None)
        proc_state["rollout_cursor"] = RolloutCursor((), discovery_complete=True)
        if auth_mode == "chatgpt":
            outcome = _pre_spawn_token_refresh(elog, aid)
            runner._spawn_token_expiry = canonical_expires_at()
            if outcome.outcome == FAILED_INVALID_GRANT:
                if not os.environ.get("OPENAI_API_KEY"):
                    raise perr.AuthTerminalError(_DEAD_GRANT_MESSAGE)
                elog.append(agent_id=aid, type="warning", payload={
                    "source": "codex_auth_fallback",
                    "message": _APIKEY_FALLBACK_MESSAGE, "detail": {}})
                # One flip: seed_auth, metered spend, and Rule A's probe gate.
                auth_mode = "apikey"
                runner._auth_mode = auth_mode
                proc_state["auth_mode"] = auth_mode
        seed_auth(mode=auth_mode, canonical_path=canonical_auth,
                  codex_home=codex_home, env=os.environ)

        # Fresh-entry audit scaffolding: step_context exactly once per agent_id.
        # Resume (balance_on_resume) re-enters and emits nothing.
        if not invocation.balance_on_resume:
            elog.append_step_context(
                agent_id=aid,
                parent_id_override=invocation.first_event_parent_id,
                provider=invocation.provider_name, model=runner.model,
                tools_schema=[{"name": n} for n in tools],
                params={"env.CODEX_HOME": str(codex_home)},
                max_tool_calls=invocation.max_tool_calls)

        # 6. Build the codex client + start/resume the persistent thread.
        client = runner._build_client(
            codex_home=codex_home, bearer_token=bearer_token,
            impty_bearer_token=(impty_bearer if runner._impty_handle is not None
                                else None))
        proc_state["web_search_enabled"] = web_enabled
        thread_options = runner._build_thread_options(invocation.tier)
        if invocation.balance_on_resume:
            from imp.engine.resume_state import InvalidResumeState
            from imp.providers.codex_rollout import restore_rollout
            if invocation.native_resume_state is None:
                raise InvalidResumeState()
            thread_id = restore_rollout(
                codex_home=codex_home,
                transcript_dir=invocation.transcript_dir,
                resume_state=invocation.native_resume_state,
            )
            proc_state["thread_id"] = thread_id
            thread = client.resume_thread(thread_id, thread_options)
        else:
            thread = client.start_thread(thread_options)
        self._thread = thread
        # Build initial prompt with anti-native-tool instruction prepended.
        next_prompt = runner._build_prompt(invocation, elog, aid)
        # Hoist rstate so the §2 resume-entry drain below can reference it safely
        # even when balance_on_resume is False (fresh-entry path has no rstate).
        rstate = None
        # Resume seam (Unit G): on a balance_on_resume re-entry, codex holds no
        # parsed message list, so derive the resume state from imp's own
        # events.jsonl. An AWAITING_USER turn (a bare-turn-end yield, detected via
        # the journaled awaiting_user_input event) solicits the human reply via
        # user_input_fn and injects it as the FIRST resumed prompt instead of the
        # system prompt; MISSING_COMPLETE_STEP (autonomous, no complete_step)
        # triggers replay-and-continue (nudge injected as the resumed prompt).
        if invocation.balance_on_resume:
            from imp.agents.classify import ResumeState
            from imp.providers.codex_resume import (
                derive_codex_resume_state,
                pending_human_reply,
            )
            rstate, last_agent_text = derive_codex_resume_state(
                invocation.events_path, aid,
                subagent_depth=invocation.subagent_depth)
            if rstate is ResumeState.AWAITING_USER:
                if invocation.user_input_fn is None:
                    self._pending_terminal = Error(payload={
                        "category": "config", "kind": "no_user_input_fn",
                        "message": "AWAITING_USER resume requires user_input_fn"})
                    return
                # §2 resume-reply join (F5): queued interjects satisfy the
                # bare-turn-end yield directly. Fall back to user_input_fn only when
                # the queue is empty. _resolve_wait_reply owns the SOLE
                # awaiting_user_input emit (fired only when it actually solicits);
                # the resume-branch duplicate is dropped (Part 1 is centralized).
                pending = pending_human_reply(invocation.events_path, aid)
                reply, source = await _resolve_wait_reply(
                    invocation.coordinator, invocation.user_input_fn,
                    invocation.resume_should_continue,
                    elog=elog, agent_id=aid,
                    resume_cause=invocation.resume_cause,
                    pending_reply=pending,
                    last_agent_message=last_agent_text,
                    halt_writer=invocation.halt_writer,
                    halt_clearer=invocation.halt_clearer)
                if source == "nudge":
                    assert isinstance(reply, str)
                    # Internal continuation, not a human turn: emit an
                    # engine_message/continuation event (no `>` caret).
                    elog.append_engine_message(
                        agent_id=aid,
                        message=Message(role="user", content=(TextBlock(text=reply),)),
                        subtype="continuation")
                    next_prompt = reply
                elif source == "pending_reply":
                    # The reply's user_message is already durable in events.jsonl
                    # from the prior attempt; re-journaling would duplicate it.
                    assert isinstance(reply, str)
                    next_prompt = reply
                else:
                    next_prompt = deliver_continuation_reply(
                        None, None, elog, aid, reply, human=True)
            elif rstate is ResumeState.MISSING_COMPLETE_STEP:
                # Replay-and-continue: nudge is the resumed prompt; journal for audit.
                nudge = continuation_nudge(invocation.resume_cause)
                nudge_msg = Message(role="user", content=(TextBlock(text=nudge),))
                elog.append_engine_message(agent_id=aid, message=nudge_msg, subtype="continuation")
                next_prompt = nudge
        if invocation.balance_on_resume and invocation.retry_feedback:
            feedback = deliver_continuation_reply(
                None, None, elog, aid, invocation.retry_feedback, human=False)
            next_prompt = f"{feedback}\n\n{next_prompt}"
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
                        message=Message(role="user", content=(TextBlock(text=t),)),
                        subtype="interject")
                if parts:
                    next_prompt = next_prompt + "\n\n" + "\n".join(parts)
        self._next_prompt = next_prompt

        self.guard = LoopGuard(interactive=invocation.interactive)
        self.ceiling = invocation.max_tool_calls  # inert: codex trips mid-stream

        # Tier A proactive-handoff watch state (Mechanism 3). handoff_phase is None
        # until the threshold crosses, then "instruction_sent" for the one
        # handoff-writing turn that follows.
        from imp.engine.reincarnation import handoff_path_for
        self._handoff_phase: str | None = None
        self._handoff_path_obj = handoff_path_for(invocation.transcript_dir)

    def _cancelled_event(self) -> object:
        self.runner._process_owner.acknowledge()
        return _CANCEL

    async def _receive(self) -> Any:
        coordinator = self.invocation.coordinator
        first = True
        while True:
            if self.runner._cancel:
                yield self._cancelled_event()
                return
            if not first:
                # Internal re-query loop-back (the handoff-crossing threshold
                # re-query set by _is_terminal): reset capture like the old loop's
                # top-of-turn reset. A nudge/continuation re-query resets via the
                # base's send()→new events() call, so the first turn does NOT.
                self._reset_capture()
            first = False
            telemetry: RolloutTelemetry = self._proc_state["rollout_telemetry"]
            thread_id = self._proc_state.get("thread_id")
            if thread_id:
                telemetry.thread_id = thread_id
            self._proc_state["rollout_cursor"] = telemetry.begin_turn()
            streamed = await self._thread.run_streamed(self._next_prompt)
            requery = False
            async with contextlib.aclosing(request_bracketed(
                    coordinator, streamed.events)) as _events:
                async for event in _events:
                    # Dead-server / cancel checks stay AHEAD of note_activity /
                    # _sweep_bg so a dead server never dereferences the coordinator.
                    if self.runner._server_handle.task.done():
                        yield _SERVER_DIED
                        return
                    if self.runner._cancel:
                        yield self._cancelled_event()
                        return
                    if coordinator is not None:
                        coordinator.note_activity()
                    # Post-API-site analog (Decision 10): one sweep per streamed
                    # codex event.
                    await _bg._sweep_bg(self.ctx)
                    yield event
                    if getattr(event, "type", None) == "turn.completed":
                        if self._requery_pending:
                            self._requery_pending = False
                            requery = True
                        break
            if self.runner._cancel:
                yield self._cancelled_event()
                return
            if requery:
                continue
            return

    async def _settle_auth_terminality(
        self, err: perr.ProviderError,
    ) -> perr.ProviderError:
        """Probe the canonical token on a codex auth error and upgrade to
        AuthTerminalError when the grant is dead. Gated on the spawn's own auth
        mode: the canonical file is not the credential an apikey spawn runs on."""
        if err.category != "auth":
            return err
        if self._proc_state.get("auth_mode") != "chatgpt":
            return err
        outcome = await asyncio.to_thread(
            refresh_canonical_token, int(time.time() * 1000), force=True,
            spawn_expires_at=self.runner._spawn_token_expiry,
        )
        _journal_token_refresh(self.elog, self.aid, outcome)
        if outcome.outcome == SKIPPED_ROTATED:
            # peer already rotated; retry re-enters _enter() and re-seeds
            err.kind = "token_rotated"  # type: ignore[attr-defined]
            return err
        if outcome.outcome != FAILED_INVALID_GRANT:
            return err
        upgraded = perr.AuthTerminalError(_DEAD_GRANT_MESSAGE)
        upgraded.detail.update(err.detail)
        upgraded.detail["provider_message"] = str(err)
        return upgraded

    async def _resolve_rollout_readings(self, completed_usage: Any) -> RolloutReadings | None:
        codex_home = self.runner._codex_home
        thread_id = self._proc_state.get("thread_id")
        if codex_home is None or not thread_id:
            return None
        telemetry: RolloutTelemetry = self._proc_state["rollout_telemetry"]
        cursor: RolloutCursor = self._proc_state["rollout_cursor"]
        telemetry.thread_id = thread_id
        return await _wait_for_current_readings(
            telemetry, cursor, _rollout_usage(completed_usage))

    async def _message_to_items(self, raw: Any) -> Any:
        if raw is _SERVER_DIED:
            sh = self.runner._server_handle
            exc = (sh.task.exception()
                   if not sh.task.cancelled() else None)
            yield Error(payload={
                "category": "network", "kind": "mcp_server_died",
                "message": "MCP server task ended mid-turn",
                "detail": {"server_error": str(exc) if exc else ""}})
            return
        if raw is _CANCEL:
            yield Error(payload={
                "category": "config", "kind": "interrupted",
                "message": "runner cancelled by interrupt"})
            return
        state = self._state
        rollout_readings = None
        if (getattr(raw, "type", None) == "turn.completed"
                and getattr(raw, "usage", None) is not None):
            rollout_readings = await self._resolve_rollout_readings(raw.usage)
        process_event(raw, elog=self.elog, agent_id=self.aid,
                      state=self._proc_state,
                      provider=self.invocation.provider_name,
                      model=self.runner.model, run_state=state,
                      codex_home=self.runner._codex_home,
                      rollout_readings=rollout_readings)

        # Mirror complete_step_args from the shared state into proc_state so the
        # terminal check sees them at the boundary.
        if state.complete_step_called:
            self._proc_state["complete_step_called"] = True
            self._proc_state["complete_step_args"] = dict(state.complete_step_args)

        # Cap trip from inside the MCP wrapper → tear the turn down + bail.
        if state.abort_requested:
            # Not a pair today; substituting is still right — the added background
            # kill is what run()'s finally does moments later anyway, and this path
            # is ending the run.
            await terminate_tool_processes(self.ctx)
            yield Error(payload={
                "category": "config",
                "kind": "tool_call_ceiling_exceeded",
                "ceiling": state.cap,
                "message":
                    f"codex tool dispatch cap ({state.cap}) exceeded"})
            return

        # turn.failed → categorize + settle terminality + bail.
        if getattr(raw, "type", None) == "turn.failed":
            err = await self._settle_auth_terminality(
                categorize_turn_failed(getattr(raw, "error", {}) or {}))
            yield Error(payload=cast(dict[str, Any], perr.make_error_event(
                err, kind=getattr(err, "kind", ""))["payload"]))
            return

    def _drain_kills(self) -> list[dict[str, Any]]:
        state = self._state
        infos: list[dict[str, Any]] = []
        while state.killed_tool_uses:
            infos.append(state.killed_tool_uses.pop(0))
        return infos

    def _fatal_error(self) -> dict[str, Any] | None:
        state = self._state
        if state.fatal_tool_error is not None:
            tool_name, msg = state.fatal_tool_error
            return {
                "category": "config", "kind": "fatal_tool_error",
                "tool": tool_name, "message": msg,
            }
        return None

    async def _is_terminal(self, raw: Any) -> bool:
        if getattr(raw, "type", None) != "turn.completed":
            return False
        from imp.engine.reincarnation import handoff_instruction, write_handoff_note
        from imp.providers.catalog import sdk_context_threshold_crossed
        # Tier A watch: codex's own reading crossed the handoff limit
        # while still working (no complete_step). Inject the handoff instruction
        # as the next turn prompt and capture the note on the following turn
        # (re-query). Completion wins over a mere crossing (old ordering: the
        # complete_step branch returned before the crossing check), so gate on
        # not-completed. No reading -> (None, None) -> never fires.
        tokens, window = self._proc_state.get("context_reading") or (None, None)
        if (self._handoff_phase is None
                and not self._complete_step_called
                and sdk_context_threshold_crossed(tokens, window)):
            instr = handoff_instruction()
            self.elog.append_engine_message(
                agent_id=self.aid,
                message=Message(role="user", content=(TextBlock(text=instr),)),
                subtype="continuation")
            self._next_prompt = instr
            self._handoff_phase = "instruction_sent"
            self._proc_state["handoff"] = True
            self._requery_pending = True
            return False
        if self._handoff_phase == "instruction_sent":
            # Handoff wins over a stray complete_step on this turn.
            write_handoff_note(
                self._handoff_path_obj, self._proc_state.get("last_agent_text", ""))
            self._handoff_done = True
            self._handoff_path = str(self._handoff_path_obj)
        return True

    def _flush_turn_record(self) -> TurnRecord:
        ps = self._proc_state
        return TurnRecord(
            assistant_text=ps.get("last_agent_text", "") or "",
            tools=list(ps.get("turn_tool_outcomes", [])),
            has_complete_step=bool(ps.get("complete_step_called")))

    async def _terminal_error(self, last_text: str) -> dict[str, Any] | None:
        classified = _classify_agent_text(
            last_text, reading=self._proc_state.get("quota_reading"),
            output_tokens=self._proc_state.get("turn_output_tokens"),
            stop_reason=None)
        if classified is not None:
            classified.detail["last_agent_text"] = last_text[:500]
            return cast(dict[str, Any], perr.make_error_event(
                classified, kind=getattr(classified, "kind", ""))["payload"])
        return None

    def _last_text(self) -> str:
        return self._proc_state.get("last_agent_text", "") or ""

    def _trailing_text(self) -> str:
        # The capture is already per-turn: _reset_capture clears it each turn.
        return self._last_text()

    def subagent_deliverable(self) -> str:
        return self._last_text()

    def _requery(self, text: Any, *, human: bool) -> None:
        self._next_prompt = deliver_continuation_reply(
            None, None, self.elog, self.aid, text, human=human)

    def _drain_interject(self) -> None:
        coordinator = self.invocation.coordinator
        if coordinator is None:
            return
        t = coordinator.drain_interject()
        if t is not None:
            self.elog.append_user_message(
                agent_id=self.aid,
                message=Message(role="user", content=(TextBlock(text=t),)),
                subtype="interject")
            self._next_prompt = f"{self._next_prompt}\n\n{t}"

    def _reset_capture(self) -> None:
        ps = self._proc_state
        ps["complete_step_called"] = False
        ps["complete_step_args"] = {}
        ps["turn_tool_outcomes"] = []
        ps["last_agent_text"] = ""
        state = self._state
        state.complete_step_called = False
        state.complete_step_args = {}
        state.task_starts = TaskStartCoordinator()


class CodexAgentRunner:
    """Drives `codex exec` through imp's own `codex_exec` driver."""

    def __init__(
        self, model_name: str, *,
        client_factory: Any = None,
        user_input_fn: Any = None,
    ) -> None:
        self.model = model_name
        self._client_factory = client_factory
        self._user_input_fn = user_input_fn
        self._process_owner = CodexProcessOwner()
        self._ctx: ToolContext | None = None
        # Test seam: overrides the canonical codex auth.json path. Read by
        # `_canonical_auth_path()`; declared here so the attribute the tests set
        # is part of the class contract.
        self._canonical_auth_override: Path | None = None
        # Per-step capture of the nested `codex` CLI's stderr. Built in
        # CodexSession._enter and published to current_stderr_sink for
        # `codex_exec`'s tee. None until built (the stubbed-loop outer-except
        # tests never build it).
        self._stderr_sink: Any = None
        # Teardown handles stashed by CodexSession._enter so run()'s finally can
        # reach them on every exit path (an abandoned session generator only
        # GC-finalizes, so teardown lives in run()'s own finally).
        self._server_handle: Any = None
        self._codex_home: Path | None = None
        self._impty_handle: Any = None
        self._impty_driver: Any = None
        self._stderr_token: Any = None
        self._auth_mode: AuthMode | None = None
        # Canonical token expiry this spawn seeded from; handed to the probe as spawn_expires_at.
        self._spawn_token_expiry: int | None = None
        self._canonical_auth: Path | None = None
        self._proc_state: dict[str, Any] = {}

    @property
    def _cancel(self) -> bool:
        return self._process_owner.cancelled

    async def run(self, invocation: AgentInvocation) -> AsyncGenerator[Event, None]:
        session = CodexSession(invocation, self)
        try:
            try:
                async with contextlib.aclosing(
                        run_agent_loop(invocation, session)) as loop:
                    async for ev in loop:
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
                    # captured nothing do we fall back to str(exc): codex_exec
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
            # A leave control cancels the run; teardown must still complete.
            with anyio.CancelScope(shield=True):
                self._process_owner.acknowledge()
                # Two nested finally blocks preserve the old structure's exception
                # isolation: the former _run_loop teardown is the inner block; the
                # former run() bg teardown is the outer finally, so it STILL runs even
                # if the (unguarded) teardown_codex_home raises. Within the inner block
                # the impty shutdown/close are Exception-suppressed, and persist_rollout
                # is wrapped so a rollout-IO failure never skips the auth-shredding
                # teardown_codex_home.
                try:
                    if self._stderr_token is not None:
                        current_stderr_sink.reset(self._stderr_token)
                    if self._stderr_sink is not None:
                        self._stderr_sink.close()
                    if self._impty_handle is not None:
                        with contextlib.suppress(Exception):
                            await self._impty_handle.shutdown()
                    if self._impty_driver is not None:
                        with contextlib.suppress(Exception):
                            self._impty_driver.close()
                    if self._server_handle is not None:
                        await self._server_handle.shutdown()
                    if self._codex_home is not None:
                        # A provisioned codex_home implies _enter reached auth setup,
                        # so canonical_auth is set (both stashed before codex_home).
                        assert self._canonical_auth is not None
                        # Own codex's native rollout across the pause BEFORE teardown
                        # deletes CODEX_HOME (Fix 1). Best-effort: a persist failure
                        # must not skip the auth-shredding teardown below. A later
                        # resume that finds no persisted rollout fails loudly in
                        # restore_rollout.
                        tid = self._proc_state.get("thread_id")
                        if tid:
                            from imp.providers.codex_rollout import persist_rollout
                            try:
                                persist_rollout(
                                    codex_home=self._codex_home, thread_id=tid,
                                    transcript_dir=invocation.transcript_dir)
                            except Exception as exc:  # persist must not skip teardown
                                _logger.warning(
                                    "codex: rollout persist failed (resume may be "
                                    "unavailable): %s", exc)
                        teardown_codex_home(
                            codex_home=self._codex_home, mode=self._auth_mode,
                            canonical_path=self._canonical_auth)
                finally:
                    if self._ctx is not None:
                        # None guard: _ctx is assigned mid-_enter (after preflight).
                        # run()'s OWN finally — an abandoned session generator only
                        # GC-finalizes (PEP 525), so bg teardown must not depend on it.
                        from imp.agents.tools.context import teardown_tool_context
                        with contextlib.suppress(Exception):
                            await teardown_tool_context(self._ctx)

    def _canonical_auth_path(self) -> Path:
        """The canonical codex `auth.json` — honors $CODEX_HOME (fallback
        ~/.codex), matching codex_token_refresh._canonical_dir so the pre-spawn
        refresh freshens exactly the file seed_auth copies from. Test seam: a
        runner attribute overrides this for the auth fixture tests."""
        override = getattr(self, "_canonical_auth_override", None)
        if override is not None:
            return Path(override)
        codex_home = os.environ.get("CODEX_HOME")
        base = Path(codex_home) if codex_home else Path.home() / ".codex"
        return base / "auth.json"

    def _build_client(
        self, *, codex_home: Path, bearer_token: str,
        impty_bearer_token: str | None = None,
    ) -> Any:
        """Construct the `codex exec` client. Tests inject a `client_factory`
        callable that takes `options=` and returns a FakeCodex."""
        if self._client_factory is not None:
            return self._client_factory(options=None)
        from imp.engine.env import overlay_engine_env
        overrides = {
            "CODEX_HOME": str(codex_home),
            "IMP_MCP_TOKEN": bearer_token,
        }
        if impty_bearer_token is not None:
            overrides["IMP_IMPTY_TOKEN"] = impty_bearer_token
        env = overlay_engine_env(os.environ, overrides)
        return Codex(CodexOptions(env=env), process_owner=self._process_owner)

    def _build_thread_options(self, tier: str | None = None) -> ThreadOptions:
        """The design's exact production flags. Lives in its own method so the
        test seam can replace it without touching `CodexSession._enter`.

        `medium` and `high` set `model_reasoning_effort` to 'high'. `low` and an
        untiered invocation leave it unset so Codex keeps its own default. `tier`
        defaults to None so the arg-less test seam works. Imp deliberately selects
        no effort above 'high'.

        There is deliberately no web-search field: codex >=0.137 deprecated the
        `features.web_search_request` flag (it no longer disables and emits a
        noisy `error` item every turn), so web_search is controlled via
        config.toml's top-level `web_search` key instead (see
        `render_config_toml`)."""
        return ThreadOptions(
            model=self.model,
            sandbox_mode="read-only",
            approval_policy=CODEX_APPROVAL_POLICY,
            model_reasoning_effort="high" if tier in ("medium", "high") else None,
        )

    def _build_prompt(self, invocation: AgentInvocation, elog: Any, aid: str) -> str:
        """The wire prompt: the agent's system prompt plus the codex tool policy,
        with the first user message folded in on fresh entry only."""
        policy = (
            "IMPORTANT — Tool policy:\n"
            "- Your imp tools are already loaded and callable directly by name\n"
            "  (`read_file`, `bash`, `complete_step`, …). Do not search for them,\n"
            "  do not route them through `exec`, and do not claim they are\n"
            "  unavailable.\n"
            "- Use ONLY your imp tools. Never call a native built-in such as\n"
            "  `apply_patch`.\n"
        )
        if invocation.subagent_depth > 0:
            policy += (
                "- Your final message is your entire deliverable to the agent\n"
                "  that dispatched you; state your full conclusion in your last\n"
                "  message.\n"
            )
        else:
            policy += (
                "- Finish the task by calling `complete_step` with the structured\n"
                "  answer.\n"
            )
        prompt = invocation.system_prompt + "\n\n" + policy
        # Codex takes a string and never reads initial_messages: fold the task
        # in on fresh entry only (resumes already carry it). Emit the taxonomy
        # ONLY on fresh entry — a resume must not re-emit system_prompt/agent_prompt
        # (the wire string is unchanged either way).
        if not invocation.balance_on_resume:
            elog.append_engine_message(
                agent_id=aid,
                message=Message(role="system",
                                content=(TextBlock(text=invocation.system_prompt),)),
                subtype="system_prompt")
            if invocation.initial_messages:
                first = invocation.initial_messages[0]
                body = "\n\n".join(
                    b.text for b in first.content if isinstance(b, TextBlock))
                if body:
                    prompt = f"{prompt}\n\n{body}"
                    elog.append_engine_message(
                        agent_id=aid,
                        message=Message(role="user", content=(TextBlock(text=body),)),
                        subtype="agent_prompt")
        return prompt

    async def interrupt(self) -> None:
        self._process_owner.cancel()
        try:
            if self._ctx is not None:
                await terminate_tool_processes(self._ctx)
        finally:
            await self._process_owner.wait_for_acknowledgement()

    async def wait_for_interrupt_ack(self) -> None:
        await self._process_owner.wait_for_acknowledgement()

    async def force_kill(self) -> None:
        await self._process_owner.force_kill()


# ── Event mapping ─────────────────────────────────────────────────────────────


# The rollout record shape is not a public codex contract: warn once per run
# when a turn that billed tokens yielded no reading.
_NO_CONTEXT_READING_WARNING = (
    "no codex context reading in the rollout; context gauge and "
    "handoff watch inactive for this turn (warned once per run)")

# codex ignores unknown top-level config keys silently, so an upstream rename of
# `model_auto_compact_token_limit` would revert imp to invisible history loss.
_COMPACTION_WARNING = (
    "codex auto-compacted its context — the model_auto_compact_token_limit "
    "pin is not in effect (warned once per run)")


def _attach_usage_to_last_agent_message(
    elog: Any, *, agent_id: str, usage: dict[str, Any],
    context_tokens: int | None = None, context_window: int | None = None,
) -> None:
    """Patch the most recent `agent_message` payload for `agent_id` with
    `usage` (and codex's live occupancy pair when supplied). Required on
    every `EventLog` implementation — see `src/imp/engine/state.py`."""
    elog.update_last_agent_message_usage(
        agent_id=agent_id, usage=usage, context_tokens=context_tokens,
        context_window=context_window)


def _serialize_mcp_result(result: Any) -> str:
    """Render a codex ``McpToolCallResult`` to text for the tool_result event.

    The wire shape is ``.content`` (a list of MCP content-block dicts) plus
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


def _flush_codex_prose(
    elog: Any, *, agent_id: str, provider: str, model: str,
    state: dict[str, Any], output_tokens: int | None,
) -> None:
    """Emit the turn's buffered prose as a prose-only `agent_message` and
    clear the buffer. No-op when the buffer is empty.
    Sets `state["turn_had_llm"]` so `turn.completed` knows whether a
    prose-less turn still needs an empty-prose usage carrier."""
    parts = state.pop("prose_buffer", [])
    if not parts:
        return
    elog.append_agent_message(
        agent_id=agent_id, provider=provider, model=model,
        raw={"content": [{"type": "text", "text": t} for t in parts]},
        usage={}, stop_reason=None, auth_mode=state.get("auth_mode"),
        provider_error=classifies_as_provider_error(
            parts[-1], output_tokens=output_tokens, stop_reason=None),
        handoff=bool(state.get("handoff")))
    state["turn_had_llm"] = True


def process_event(
    event: Any, *, elog: Any, agent_id: str, state: dict[str, Any],
    provider: str, model: str, run_state: Any = None,
    codex_home: Path | None = None,
    rollout_readings: RolloutReadings | None = None,
) -> None:
    """Translate one codex ThreadEvent into imp EventLog records.

    `state` is a shared dict carrying in-flight thread_id, the latest context
    reading, captured complete_step args, etc.

    The default arm (unknown item type) logs a runner_warning and returns; so
    does an item imp's own event models could not parse (see the parse-boundary
    bullet under Layering invariants in docs/internals/invariants.md). Only
    security-relevant types get special handling.
    """
    etype = getattr(event, "type", None)

    # A payload codex_events could not validate arrives degraded, carrying only
    # its id/type. Ahead of the etype dispatch so item.started/updated/completed
    # are covered alike; one warning per event, no seen-id state.
    degraded = getattr(event, "item", None)
    parse_error = getattr(degraded, "imp_parse_error", None)
    if parse_error is not None:
        elog.append(agent_id=agent_id, type="runner_warning", payload={
            "message": f"codex {getattr(degraded, 'type', '')} item did not match imp's codex "
                       f"exec event model and was skipped",
            "codex_parse_error": parse_error})
        return

    # `thread.started` — capture thread_id for resume / re-query.
    if etype == "thread.started":
        state["thread_id"] = getattr(event, "thread_id", None)
        return

    if etype == "item.started":
        item = getattr(event, "item", None)
        name = getattr(item, "tool", "") or getattr(item, "name", "") or ""
        if getattr(item, "type", None) == "mcp_tool_call" and name == "sub_agent":
            _flush_codex_prose(elog, agent_id=agent_id, provider=provider,
                               model=model, state=state, output_tokens=None)
            if run_state is not None:
                run_state.task_starts.task_started()
        return

    # `item.completed` — dispatch on the item's own type.
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
            # Buffer prose; it is emitted as a prose-only agent_message at the
            # next text->tool transition (or at turn.completed for trailing /
            # text-only prose). tool_use lives only in the standalone tool_call
            # events, so a turn's prose can lead its tool_calls.
            if text:
                # Split at the classifying boundary so error prose gets its own node.
                if matches_error_prose(text) and state.get("prose_buffer"):
                    _flush_codex_prose(elog, agent_id=agent_id, provider=provider,
                                       model=model, state=state, output_tokens=None)
                state.setdefault("prose_buffer", []).append(text)
            return
        if itype == "mcp_tool_call":
            # text->tool transition: a preamble's prose must lead this call's
            # tool_call. For a task (sub_agent), item.started already flushed the
            # buffer, so this completion-side flush is a no-op.
            _flush_codex_prose(elog, agent_id=agent_id, provider=provider,
                               model=model, state=state, output_tokens=None)
            # codex's `McpToolCallItem` exposes `tool` + `server`, NOT `name`;
            # reading `name` silently recorded "" for every call (and made the
            # complete_step capture below dead). Fall back to `name` only for
            # duck-typed test shapes.
            name = getattr(item, "tool", "") or getattr(item, "name", "") or ""
            args = getattr(item, "arguments", None) or {}
            item_id = getattr(item, "id", "") or ""
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
            call_id = item_id
            if name != "sub_agent":
                elog.append_tool_call(
                    agent_id=agent_id,
                    block=ToolUseBlock(id=call_id, name=name, input=args),
                )
                elog.append_tool_result(
                    agent_id=agent_id,
                    block=ToolResultBlock(
                        tool_use_id=call_id, content=content, is_error=is_error,
                    ),
                )
            if name == "save_and_exit" and not is_error:
                state["save_and_exit_logged"] = True
            if name == "complete_step":
                # Fallback capture from the RAW codex item args. When the
                # in-process wrapper's RunState capture ran, the consume loop's
                # mirror overwrites proc_state with those args; this write is
                # the fallback for when that path didn't run.
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
            return
        if itype == "web_search":
            query = getattr(item, "query", "") or ""
            if state.get("web_search_enabled"):
                # Expected: the agent declared `web_search` and codex ran the
                # model-hosted search. Surface it as a tool_call so it shows in
                # the narrative/`imp log`. codex's WebSearchItem exposes only the
                # query (no results/citations), so the result is informational.
                call_id = getattr(item, "id", "") or ""
                # text->tool transition: lead the tool_call with any buffered prose.
                _flush_codex_prose(elog, agent_id=agent_id, provider=provider,
                                   model=model, state=state, output_tokens=None)
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
                return
            # Disabled in config.toml: the tool should be gone. Seeing one is
            # a soft tripwire (not fail-closed by itself; the disable is
            # verified live in the e2e test).
            elog.append(agent_id=agent_id, type="runner_warning",
                        payload={"message":
                                 f"unexpected codex web_search item "
                                 f"(query={query!r})"})
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
        # Read usage FIRST: the trailing flush needs this turn's token count, and a
        # usage-less turn must not leave the previous turn's count readable below.
        usage = getattr(event, "usage", None)
        output_tokens = (None if usage is None
                         else int(getattr(usage, "output_tokens", 0) or 0))
        state["turn_output_tokens"] = output_tokens
        # Flush any trailing prose (a post-tool summary, or a text-only turn) as
        # a prose-only agent_message BEFORE usage attaches. A prose-less tool-only
        # turn emitted no agent_message this turn, so synthesize an empty-prose
        # carrier to hold the usage patch — update_last_agent_message_usage would
        # otherwise land on the PRIOR turn's response (or raise on turn one).
        # tool_use never rides in a codex agent_message now; it lives only in the
        # standalone tool_call events.
        _flush_codex_prose(elog, agent_id=agent_id, provider=provider,
                           model=model, state=state, output_tokens=output_tokens)
        turn_had_llm = state.pop("turn_had_llm", False)
        if usage is None:
            return
        if not turn_had_llm:
            elog.append_agent_message(
                agent_id=agent_id, provider=provider, model=model,
                raw={"content": []}, usage={}, stop_reason=None,
                auth_mode=state.get("auth_mode"),
            )
        # Map flat codex keys explicitly. The existing `_normalize_usage`
        # OpenAI-shape detector reads `input_tokens_details.cached_tokens`
        # (nested) — codex emits a FLAT `cached_input_tokens` instead, so
        # without this mapping cache would silently record as zero.
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        cached = int(getattr(usage, "cached_input_tokens", 0) or 0)
        # Like `cached`, cache-write nests inside input_tokens, so both
        # subtract from fresh.
        cache_write = int(getattr(usage, "cache_write_input_tokens", 0) or 0)
        reasoning_tokens = int(getattr(usage, "reasoning_output_tokens", 0) or 0)
        normalized = {
            # `input_tokens` here means FRESH input (anthropic-style) so the
            # downstream `_normalize_usage` anthropic branch picks it up
            # correctly: fresh = anthropic input, cache_read = cache.
            "input_tokens": max(0, input_tokens - cached - cache_write),
            "output_tokens": output_tokens,
            "cache_read_input_tokens": cached,
            "cache_creation_input_tokens": cache_write,
            "reasoning_output_tokens": reasoning_tokens,
        }
        # `normalized` is a billing sum over every model request in this turn,
        # not prompt occupancy — use the correlated per-request record.
        thread_id = state.get("thread_id")
        readings = rollout_readings or RolloutReadings(context=None, quota=None)
        reading = readings.context
        state["context_reading"] = reading
        if readings.quota is not None:
            state["quota_reading"] = readings.quota
            elog.append_quota_reading(agent_id=agent_id, reading=readings.quota)
        if reading is None and not state.get("context_canary_emitted"):
            state["context_canary_emitted"] = True
            elog.append(agent_id=agent_id, type="runner_warning",
                        payload={"message": _NO_CONTEXT_READING_WARNING})
        if (codex_home is not None and thread_id
                and not state.get("compaction_canary_emitted")):
            try:
                compacted = compaction_seen(codex_home, thread_id)
            except OSError:  # codex writes this file concurrently
                compacted = False
            if compacted:
                state["compaction_canary_emitted"] = True
                elog.append(agent_id=agent_id, type="runner_warning",
                            payload={"message": _COMPACTION_WARNING})
        tokens, window = reading if reading is not None else (None, None)
        _attach_usage_to_last_agent_message(
            elog, agent_id=agent_id, usage=normalized,
            context_tokens=tokens, context_window=window)
        return

    # Default arm: turn.failed / error / unknown —
    # handled by subsequent tasks. Falling through here is benign.


# ── Error categorization ──────────────────────────────────────────────────────


def categorize_turn_failed(error: dict[str, Any] | Any) -> perr.ProviderError:
    """Map a codex ``turn.failed.error`` body into an imp ``ProviderError`` subclass.

    Known cases:
      - HTTP 401 / "Unauthorized" → ``AuthError``
      - OAuth refresh-token reuse ("already used" / "log out and sign in") → ``AuthError``
      - HTTP 400 "model … not supported … ChatGPT account" → ``ConfigError``
      - model context overflow → ``ContextWindowError`` (Tier B reincarnation)
      - selected model capacity exhaustion → ``NetworkError``
      - subprocess network/timeout → ``NetworkError``
      - any other → ``ApiError`` (terminal; maps to Pause).

    A non-dict body is normalized to ``str(error)`` and runs the same chain, so no
    error shape can bypass classification.
    """
    if isinstance(error, dict):
        status = error.get("status")
        message = str(error.get("message", "") or "")
        kind = str(error.get("kind", "") or "")
    else:
        status, message, kind = None, str(error), ""

    if status == 401 or "unauthorized" in message.lower():
        return perr.AuthError(f"codex: {message[:200]}")
    if "already used" in message.lower() or "log out and sign in" in message.lower():
        return perr.AuthError(f"codex: {message[:200]}")
    if status == 400 and "chatgpt account" in message.lower():
        return perr.ConfigError(f"codex: {message[:300]}")
    if any(s in message.lower() for s in (
            "context_length_exceeded", "maximum context length",
            "context window", "too many tokens")):
        return perr.ContextWindowError(f"codex: {message[:200]}")
    if "selected model is at capacity. please try a different model." in message.lower():
        return perr.NetworkError(f"codex: {message[:200]}")
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
