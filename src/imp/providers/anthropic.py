"""Anthropic provider + runner (spec §6.9). Direct anthropic.AsyncAnthropic.

Loop:
  1. Build messages (transcript or initial_messages)
  2. POST to messages.stream with declared tools; pulse coordinator per event
  3. Append assistant response to transcript; append llm_response to EventLog
  4. For each tool_use block: dispatch tool, append tool_result to EventLog
  5. complete_step catches StepComplete → validate, emit agent_output, return
  6. No tool_use in assistant: nudge via continuation_fn; else error

Yields ONLY control signals: agent_output, error.
All audit events (step_context, user_message, llm_response, tool_call,
tool_result, thinking, runner_warning, tool_killed) are written via EventLog.
"""

from __future__ import annotations

import contextlib
import inspect
import os
import time
from collections.abc import AsyncGenerator
from typing import Any, cast

import anyio

from imp.agents.classify import ResumeState, classify_resume
from imp.agents.dispatch import TurnState, dispatch_turn_tool_uses
from imp.agents.loop_guard import LoopGuard, ToolOutcome, TurnRecord, _result_preview
from imp.agents.runner import (
    AgentInvocation,
    AgentRunner,
    Diagnostics,
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
from imp.agents.tools.context import ToolContext, change_home_path
from imp.agents.tools.load_skill import make_load_skill_tool
from imp.agents.transcript import (
    Transcript,
    balance_success_on_resume,
    deliver_reply_on_resume,
    repair_dangling_tool_use_on_resume,
)
from imp.engine.agent_run import (
    _INTERRUPT_CONTINUATION_NUDGE,
    _resolve_wait_reply,
    deliver_interject,
    drain_all_interjects_at_resume,
)
from imp.engine.awaiting_user_input import AwaitingUserInput
from imp.providers import errors as perr
from imp.providers.base import Provider
from imp.providers.history_compaction import is_cold
from imp.providers.message_eviction import apply_eviction, evicted_result_ids
from imp.providers.resolve import spawn_subagent_via_registry
from imp.providers.web_native import (
    WEB_TOOL_NAMES,
    anthropic_beta_headers,
    anthropic_web_tool_specs,
    check_web_capability,
)


def _classify_anthropic_exception(exc: BaseException) -> perr.ProviderError:
    """Translate an Anthropic SDK exception into the matching
    ProviderError subclass. Re-raises anything that doesn't fit a
    known category — the runner's top-level handler turns that into
    an 'unknown' error event.
    """
    from anthropic import (
        APIConnectionError,
        APIStatusError,
        APITimeoutError,
        AuthenticationError,
        BadRequestError,
        InternalServerError,
        NotFoundError,
        PermissionDeniedError,
    )
    from anthropic import RateLimitError as _AnthropicRateLimit

    if isinstance(exc, AuthenticationError):
        request_id = getattr(exc, "request_id", None)
        detail: dict[str, object] = {"request_id": request_id} if request_id else {}
        return perr.AuthError(str(exc), detail=detail)
    if isinstance(exc, PermissionDeniedError):
        return perr.AuthError(str(exc))
    if isinstance(exc, _AnthropicRateLimit):
        retry_after: float | None = None
        resp = getattr(exc, "response", None)
        if resp is not None:
            headers = getattr(resp, "headers", None) or {}
            raw = headers.get("retry-after") if hasattr(headers, "get") else None
            if raw:
                try:
                    retry_after = float(raw)
                except (TypeError, ValueError):
                    retry_after = None
        detail = (
            {"retry_after_seconds": retry_after} if retry_after is not None else {}
        )
        return perr.RateLimitError(str(exc), detail=detail)
    if isinstance(exc, (APIConnectionError, APITimeoutError, InternalServerError)):
        return perr.NetworkError(str(exc))
    if isinstance(exc, NotFoundError):
        return perr.ConfigError(f"model not found: {exc}")
    if isinstance(exc, BadRequestError):
        msg = str(exc)
        msg_low = msg.lower()
        if "prompt is too long" in msg or "context_length_exceeded" in msg:
            return perr.ContextWindowError(msg)
        if "credit balance" in msg_low or "billing" in msg_low:
            return perr.QuotaError(msg)
        return perr.ConfigError(msg)
    if isinstance(exc, APIStatusError):
        return perr.NetworkError(f"{exc.status_code}: {exc}")
    raise exc


def _build_tools(invocation: AgentInvocation) -> dict[str, Tool]:
    """Build the per-step tool dict.

    `task` is added when the agent declares subagents — note it bypasses the
    runner's normal Tool.invoke path (see dispatch loop below).
    """
    reg = get_registry()
    out: dict[str, Tool] = {}
    check_web_capability(invocation.provider_name, invocation.agent_def)
    for name in invocation.agent_def.tools:
        if name == "complete_step":
            continue
        if name == "sub_agent":
            # Handled specially below; do NOT pull from registry.
            continue
        if name in WEB_TOOL_NAMES:
            # Reserved native-capability names (§4): never registry-looked-up;
            # routed to the native-injection path in _run_loop.
            continue
        if name == "load_skill":
            # Auto-injected below; tolerate a redundant explicit declaration.
            continue
        if name not in reg:
            raise KeyError(f"agent declares unknown tool {name!r}")
        out[name] = reg[name]
    if "sub_agent" in invocation.agent_def.tools:
        # Always register `task` if the agent declares it. If subagent_defs is
        # empty, dispatch_task raises a clear "subagent X not declared" error
        # the first time the LLM invokes `task` — much better than silently
        # advertising no tool at all and letting the LLM fumble.
        out["sub_agent"] = make_task_tool(invocation.tiers)
    out["complete_step"] = make_complete_step_tool(invocation.agent_def.output_schema)
    out["load_skill"] = make_load_skill_tool()
    if "bash" in invocation.agent_def.tools:
        # Auto-granted with bash (precedent: load_skill injection):
        # run_in_background is a bash PARAMETER, so an agent with bash but no
        # read/kill tool is a trap — a backgrounded process it can never see
        # or stop. The capability set must be atomic.
        out["bash_output"] = reg["bash_output"]
        out["bash_kill"] = reg["bash_kill"]
    return out


def _anthropic_tool_schema(t: Tool) -> dict[str, Any]:
    return {"name": t.name, "description": t.description, "input_schema": t.input_schema}


def _message_to_anthropic(msg: Message) -> dict[str, Any]:
    content: list[dict[str, Any]] = []
    for b in msg.content:
        if isinstance(b, TextBlock):
            content.append({"type": "text", "text": b.text})
        elif isinstance(b, ToolUseBlock):
            content.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
        elif isinstance(b, ToolResultBlock):
            content.append({
                "type": "tool_result", "tool_use_id": b.tool_use_id,
                "content": b.content, "is_error": b.is_error,
            })
    return {"role": msg.role, "content": content}


def _serialize_anthropic_response(response: Any) -> dict[str, Any]:
    """Serialize a provider response to a plain dict (the llm_response.raw
    value). Prefers SDK pydantic .model_dump() for max fidelity; falls back to
    introspecting .content / .stop_reason / .usage for test fakes. Both paths
    produce {content: [block-dicts], stop_reason, usage, model}."""
    dump = getattr(response, "model_dump", None)
    if callable(dump):
        try:
            return cast(dict[str, Any], dump(mode="json"))
        except TypeError:
            return cast(dict[str, Any], dump())
    content: list[dict[str, Any]] = []
    for block in getattr(response, "content", []):
        kind = getattr(block, "type", None)
        if kind == "text":
            content.append({"type": "text", "text": block.text})
        elif kind == "tool_use":
            content.append({"type": "tool_use", "id": block.id,
                            "name": block.name, "input": block.input})
        elif kind == "thinking":
            content.append({"type": "thinking",
                            "thinking": getattr(block, "thinking", "")})
        else:
            content.append({"type": str(kind)})
    return {
        "content": content,
        "stop_reason": getattr(response, "stop_reason", None),
        "usage": _usage_dict(getattr(response, "usage", None)),
        "model": getattr(response, "model", None),
    }


def _usage_dict(usage: Any) -> dict[str, Any]:
    if usage is None:
        return {}
    dump = getattr(usage, "model_dump", None)
    if callable(dump):
        return cast(dict[str, Any], dump())
    if isinstance(usage, dict):
        return usage
    return {k: getattr(usage, k) for k in
            ("input_tokens", "output_tokens",
             "cache_read_input_tokens", "cache_creation_input_tokens")
            if getattr(usage, k, None) is not None}


def _parse_anthropic_message(raw: dict[str, Any]) -> Message:
    """raw → assistant Message. Reconstruction-critical; shared with replay.py.
    Thinking + unknown blocks are dropped (they're not conversation history)."""
    content: list[Any] = []
    for block in raw.get("content", []):
        kind = block.get("type")
        if kind == "text":
            content.append(TextBlock(text=block.get("text", "")))
        elif kind == "tool_use":
            content.append(ToolUseBlock(id=block["id"], name=block["name"],
                                        input=block.get("input", {})))
        # thinking + unknown: skipped (see diagnostics)
    return Message(role="assistant", content=tuple(content))


def _extract_anthropic_diagnostics(raw: dict[str, Any]) -> Diagnostics:
    """Render-only: thinking contents + unknown-block warnings."""
    _WEB_BLOCK_TYPES = (
        "server_tool_use", "web_search_tool_result", "web_fetch_tool_result")
    warnings: list[str] = []
    thinkings: list[str] = []
    for block in raw.get("content", []):
        kind = block.get("type")
        if kind in ("text", "tool_use"):
            continue
        if kind in _WEB_BLOCK_TYPES:
            # Surfaced as normalized web events by _emit_anthropic_web_events
            # (Task 8); not conversation history, not a warning.
            continue
        if kind == "thinking":
            thinkings.append(str(block.get("thinking", "") or ""))
        else:
            warnings.append(f"skipped unknown block type {kind!r}")
    return Diagnostics(block_warnings=tuple(warnings), thinkings=tuple(thinkings))


def _collect_text_citations(blocks: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """url -> {title?, cited_text?} from every text block's citations array."""
    out: dict[str, dict[str, Any]] = {}
    for b in blocks:
        if b.get("type") != "text":
            continue
        for c in b.get("citations") or []:
            url = c.get("url")
            if not url:
                continue
            out[url] = {"title": c.get("title"), "cited_text": c.get("cited_text")}
    return out


_WEB_ERROR_TYPES = frozenset({
    "web_search_tool_result_error",
    "web_fetch_tool_result_error",
})


def _normalize_web_result(
    block: dict[str, Any], text_cites: dict[str, dict[str, Any]],
) -> tuple[str, list[dict[str, Any]], bool]:
    """Return (audit_text, citations[{url,title?,cited_text?}], is_error) for one
    web_search_tool_result / web_fetch_tool_result block.

    Error variants — a dict whose ``type`` is one of ``_WEB_ERROR_TYPES`` or
    that carries an ``error_code`` key — are returned with ``is_error=True``
    and an empty citations list so the audit trail accurately reflects the
    failure instead of recording a blank successful result.
    """
    content = block.get("content")
    citations: list[dict[str, Any]] = []
    lines: list[str] = []

    # --- error branch (BOTH search and fetch errors arrive as dicts) ---
    if isinstance(content, dict) and (
        content.get("type") in _WEB_ERROR_TYPES or "error_code" in content
    ):
        error_code = content.get("error_code")
        return f"web error: {error_code}", [], True

    def _cite(url: str, title: Any) -> dict[str, Any]:
        c: dict[str, Any] = {"url": url}
        if title:
            c["title"] = title
        extra = text_cites.get(url) or {}
        if extra.get("cited_text"):
            c["cited_text"] = extra["cited_text"]
        return c

    if isinstance(content, list):  # web_search_tool_result (success)
        for r in content:
            if not isinstance(r, dict) or not r.get("url"):
                continue
            url = r["url"]
            title = r.get("title")
            citations.append(_cite(url, title))
            lines.append(f"{title} — {url}" if title else url)
    elif isinstance(content, dict):  # web_fetch_tool_result (success)
        url = content.get("url") or ""
        doc = content.get("content")
        title = doc.get("title") if isinstance(doc, dict) else None
        if url:
            citations.append(_cite(url, title))
            lines.append(url)
        if isinstance(doc, dict):
            source = doc.get("source")
            if (isinstance(source, dict) and source.get("type") == "text"
                    and isinstance(source.get("data"), str)):
                lines.append(source["data"])
    return "\n".join(lines), citations, False


def _emit_anthropic_web_events(
    raw: dict[str, Any], elog: Any, aid: str,
) -> None:
    """Side-path emission of normalized web events from a serialized response.
    Walks blocks in document order so each server_tool_use `tool_call` is
    immediately followed by its `tool_result`. Never dispatches anything —
    the provider already executed server-side."""
    blocks = raw.get("content", [])
    text_cites = _collect_text_citations(blocks)
    for b in blocks:
        kind = b.get("type")
        if kind == "server_tool_use" and b.get("name") in ("web_search", "web_fetch"):
            elog.append_tool_call(
                agent_id=aid,
                block=ToolUseBlock(id=b.get("id", ""), name=b["name"],
                                   input=b.get("input") or {}))
        elif kind in ("web_search_tool_result", "web_fetch_tool_result"):
            text, citations, is_error = _normalize_web_result(b, text_cites)
            elog.append_tool_result(
                agent_id=aid,
                block=ToolResultBlock(b.get("tool_use_id", ""), text,
                                      is_error=is_error),
                citations=citations)


class AnthropicAgentRunner:
    def __init__(
        self, model_name: str,
        *,
        client_factory: Any = None,
        user_input_fn: Any = None,
    ) -> None:
        self.model = model_name                     # public per Provider protocol
        self._client_factory = client_factory
        self._user_input_fn = user_input_fn
        self._client: Any = None
        self._cancel = False
        self._cancel_scope: Any = None
        self._ctx: ToolContext | None = None

    def _get_client(self) -> Any:
        if self._client is None:
            if self._client_factory is not None:
                self._client = self._client_factory()
            else:
                import anthropic
                self._client = anthropic.AsyncAnthropic()
        return self._client

    async def _close_client(self) -> None:
        """Close the underlying SDK client if we own it and it supports close().

        Real `anthropic.AsyncAnthropic` owns an httpx pool; without explicit
        close() the pool's async teardown can race with pytest-anyio's
        per-test loop teardown and surface "Event loop is closed" errors in
        later tests. Test fakes don't expose `.close()` — skip safely.
        """
        import contextlib
        if self._client is None:
            return
        close = getattr(self._client, "close", None)
        if close is not None:
            with contextlib.suppress(Exception):
                await close()
        self._client = None

    async def run(self, invocation: AgentInvocation) -> AsyncGenerator[Event, None]:
        try:
            try:
                async for ev in self._run_loop(invocation):
                    yield ev
            except perr.ProviderError as exc:
                yield perr.make_error_event(exc)
                return
            except AwaitingUserInput:
                raise  # bare-turn-end / resume-solicit pause unwinds, not classified
            except Exception as exc:
                raise_if_suspended(exc)  # subagent-origin pause must unwind, not classify
                if self._cancel:
                    yield {"type": "error", "payload": {
                        "category": "config",
                        "kind": "interrupted",
                        "message": "runner cancelled by interrupt",
                    }}
                    return
                try:
                    classified = _classify_anthropic_exception(exc)
                except Exception:
                    yield perr.make_unknown_error_event(exc)
                    return
                yield perr.make_error_event(classified)
                return
        finally:
            if self._ctx is not None:
                # None guard: self._ctx is None-initialized and assigned
                # mid-_run_loop, AFTER the _build_tools/check_web_capability
                # raise sites — the finally runs with None when the generator
                # is aclosed before its first pull or those early raises fire.
                from imp.agents.tools.bg import terminate_background_processes
                with contextlib.suppress(Exception):
                    await terminate_background_processes(self._ctx)
            await self._close_client()

    async def _run_loop(
        self, invocation: AgentInvocation,
    ) -> AsyncGenerator[Event, None]:
        from imp.engine.state import JsonlEventLog
        elog = invocation.event_log or JsonlEventLog(invocation.events_path)
        aid = invocation.agent_id or "step:unnamed"

        transcript = Transcript(invocation.transcript_dir / "transcript.jsonl")
        tools = _build_tools(invocation)
        ctx = ToolContext(
            project_root=invocation.change_context.project_root,
            events_path=invocation.events_path,
            subagent_defs=invocation.subagent_defs,
            provider_name=invocation.provider_name,
            model_name=invocation.model_name,
            parent_transcript_dir=invocation.transcript_dir,
            agent_id=aid,
            background_enabled=True,  # full bg surface wired: injection + sweeps + teardown
            change_home=change_home_path(invocation.change_context.change_home),
        )
        ctx.change_context = invocation.change_context  # for subagent dispatch
        ctx.change_state = invocation.change_state
        ctx.change = invocation.change          # live Change for rename_change
        ctx.spawn_subagent = spawn_subagent_via_registry
        ctx.resolved_tiers = invocation.resolved_tiers
        ctx.subagent_depth = invocation.subagent_depth
        ctx.coordinator = invocation.coordinator
        ctx.tiers = invocation.tiers
        ctx.test_gates = invocation.test_gates
        ctx.checkpoint_threshold = invocation.checkpoint_threshold
        ctx.non_interactive = invocation.non_interactive
        ctx.sandbox_policy = invocation.sandbox_policy
        ctx.event_log = elog                       # shared write instance
        if self._user_input_fn is not None:
            ctx.user_input_fn = self._user_input_fn
        self._ctx = ctx

        existing = transcript.load()
        if existing:
            messages = existing
            tool_calls_used = 0  # per-invocation; never inherit history
            # Mid-tool Ctrl+C (spec §6.15) can leave a dangling tool_use when
            # the executor's pause check fires after `tool_result` was yielded
            # but before the user message was appended. Patch the transcript
            # before classifying.
            if invocation.balance_on_resume:
                balance_success_on_resume(
                    messages, transcript, elog, aid,
                    wrapper_text=invocation.retry_feedback)
            else:
                synth = repair_dangling_tool_use_on_resume(messages, transcript)
                if synth is not None:
                    for rb in synth:
                        elog.append_tool_result(agent_id=aid, block=rb)
            state = classify_resume(messages)
            if state is ResumeState.DANGLING_TOOL_USE:
                yield {"type": "error", "payload": {
                    "category": "config",
                    "kind": "transcript_invalid",
                    "message": "transcript ended with dangling tool_use",
                }}
                return
            if state is ResumeState.AWAITING_USER:
                if ctx.user_input_fn is None:
                    yield {"type": "error", "payload": {
                        "category": "config",
                        "kind": "no_user_input_fn",
                        "message": (
                            "AWAITING_USER resume requires user_input_fn; "
                            "none installed by runner"
                        ),
                    }}
                    return
                # A bare turn-end carries no separate question payload: the
                # agent's prose IS the question, already journaled and rendered.
                # Emit an empty prompt.
                elog.append(agent_id=aid, type="awaiting_user_input",
                            payload={"prompt": ""})
                # Queued interjects satisfy the pending yield directly; fall back
                # to user_input_fn only when the queue is empty.
                reply: str = await _resolve_wait_reply(
                    invocation.coordinator, ctx.user_input_fn,
                    invocation.resumed_from_interrupt)
                deliver_reply_on_resume(
                    messages, transcript, elog, aid, reply)
                # Fall through (do NOT return): the reply drives the next LLM
                # turn as the transcript's trailing user message.
            # §2 resume-entry drain: mid-turn pending texts fold into the first
            # request. (The AWAITING_USER dangling-wait join is handled above.)
            if state is not ResumeState.AWAITING_USER:
                drained = drain_all_interjects_at_resume(
                    invocation.coordinator, messages, transcript, elog, aid)
                # A mid-tool Ctrl-C resumes as SEND_TO_LLM (the dangling call was
                # repaired with a synthetic result), never AWAITING_USER, so the
                # _resolve_wait_reply nudge above can't reach it. Append the same
                # continuation nudge as the trailing user turn so a no-message
                # resume continues the step instead of bare-turn-ending on a
                # clarifying question. A drained human interject already drives
                # continuation, so it wins (parity with AWAITING_USER).
                if invocation.resumed_from_interrupt and drained == 0:
                    deliver_reply_on_resume(
                        messages, transcript, elog, aid,
                        _INTERRUPT_CONTINUATION_NUDGE)
        else:
            messages = []
            for m in invocation.initial_messages:
                messages.append(m)
                transcript.append(m)
            tool_calls_used = 0
            # Fresh-entry audit scaffolding. Ordered IMMEDIATELY after the
            # transcript append so the "step_context emitted exactly once per
            # agent_id" invariant holds (resume takes the branch above and
            # emits nothing).
            elog.append_step_context(
                agent_id=aid, parent_id_override=invocation.first_event_parent_id,
                provider=invocation.provider_name, model=self.model,
                system_prompt=invocation.system_prompt,
                tools_schema=[_anthropic_tool_schema(t) for t in tools.values()],
                params={"max_tokens": 8192},
                max_tool_calls=invocation.max_tool_calls)
            for m in invocation.initial_messages:
                if all(isinstance(b, TextBlock) for b in m.content):
                    elog.append_user_message(agent_id=aid, message=m)

        # max_tool_calls is opt-in; None means no cap. The engine resolves
        # agent_def -> workflow.defaults -> None and passes the result via
        # invocation.max_tool_calls.
        ceiling = invocation.max_tool_calls  # may be None (no cap)
        guard = LoopGuard()

        web_specs = anthropic_web_tool_specs(invocation.agent_def)
        web_headers = anthropic_beta_headers(invocation.agent_def)

        # Tier A proactive-handoff watch state (Mechanism 3). handoff_phase is
        # None until the threshold crosses, then "instruction_sent" for the one
        # handoff-writing turn that follows.
        from imp.engine.reincarnation import handoff_instruction, handoff_path_for
        from imp.providers.catalog import (
            context_threshold_crossed,
            context_tokens_from_usage,
        )
        handoff_phase: str | None = None
        handoff_path = handoff_path_for(invocation.transcript_dir)

        # Cold-cache eviction: computed ONCE here and reapplied each round so
        # the evicted prefix region stays byte-stable across tool-rounds.
        evicted_ids = evicted_result_ids(
            messages, cold=is_cold(invocation.events_path))

        while not self._cancel:
            anthropic_tools = [_anthropic_tool_schema(t) for t in tools.values()]
            anthropic_tools.extend(web_specs)
            client = self._get_client()
            create_kwargs: dict[str, Any] = dict(
                model=self.model,
                system=invocation.system_prompt,
                tools=anthropic_tools,
                messages=[
                    _message_to_anthropic(m)
                    for m in apply_eviction(messages, evicted_ids)
                ],
                max_tokens=8192,
                cache_control={"type": "ephemeral"},
                # §4: explicitly PERMIT parallel tool calls per assistant message
                # so multiple `task` tus can arrive in one turn. Unconditional and
                # safe — every in-process request carries tools (complete_step is
                # injected at :158), and Anthropic rejects tool_choice only on a
                # tools-less request, which has no in-process path. A future forced
                # tool_choice must compose disable_parallel_tool_use deliberately.
                tool_choice={"type": "auto", "disable_parallel_tool_use": False},
            )
            if web_headers:
                create_kwargs["extra_headers"] = web_headers
            # N4 race note: if interrupt() lands AFTER `while not self._cancel`
            # passes but BEFORE this assignment, this one turn's request isn't
            # cancelled — the loop simply exits on the next iteration via _cancel.
            # Worst case is one extra in-flight request; cancellation is not
            # instantaneous, by design.
            _t0 = time.perf_counter()
            self._cancel_scope = anyio.CancelScope()
            response = None
            request_id: str | None = None
            try:
                with self._cancel_scope:
                    async with client.messages.stream(**create_kwargs) as stream:
                        async for _event in stream:
                            if invocation.coordinator is not None:
                                invocation.coordinator.note_activity()
                        response = await stream.get_final_message()
                        # Streaming final messages carry no _request_id; the
                        # public stream.request_id reads the `request-id` header.
                        request_id = stream.request_id
            finally:
                # N3: reset even when the request raises a genuine API error, so a
                # later interrupt() never calls .cancel() on a stale exited scope.
                self._cancel_scope = None
            if response is None:
                # interrupt() cancelled the in-flight request — exit the loop and
                # fall through to the "cancelled (interrupt)" yield below.
                break

            raw = _serialize_anthropic_response(response)
            assistant_msg = _parse_anthropic_message(raw)
            diag = _extract_anthropic_diagnostics(raw)
            for content in diag.thinkings:
                elog.append(agent_id=aid, type="thinking",
                            payload={"content": content})
            for w in diag.block_warnings:
                elog.append(agent_id=aid, type="runner_warning",
                            payload={"message": w})
            messages.append(assistant_msg)
            transcript.append(assistant_msg)
            usage = raw.get("usage") or {}
            elog.append_llm_response(
                agent_id=aid, provider=invocation.provider_name,
                model=raw.get("model") or self.model,
                raw=raw, usage=usage,
                stop_reason=raw.get("stop_reason"),
                context_tokens=context_tokens_from_usage(usage),
                request_id=request_id,
                duration_ms=(time.perf_counter() - _t0) * 1000.0)
            _emit_anthropic_web_events(raw, elog, aid)
            # Post-API sweep + heartbeat (Decision 10): the model-API call is
            # the longest awaitable with no registry access — sweeping here
            # narrows the cap's routine inter-sweep gap to API latency. The
            # heartbeat decouples the watchdog margin from API latency.
            await _bg._sweep_bg(ctx)
            yield {"type": "tool_heartbeat", "payload": {}}

            # Tier A watch: compare this turn's occupancy to the checkpoint
            # threshold (only before a handoff is already in flight).
            crossed_now = (
                handoff_phase is None
                and invocation.checkpoint_threshold is not None
                and context_threshold_crossed(
                    raw.get("usage"), self.model,
                    invocation.checkpoint_threshold))

            tool_uses = [b for b in assistant_msg.content if isinstance(b, ToolUseBlock)]
            if not tool_uses:
                if handoff_phase == "instruction_sent":
                    # The handoff-writing turn ended without a tool: signal the
                    # captured handoff and stop (the engine reincarnates).
                    yield {"type": "handoff",
                           "payload": {"handoff_path": str(handoff_path)}}
                    return
                last_text = "".join(
                    b.text for b in assistant_msg.content
                    if isinstance(b, TextBlock)
                )
                cont = invocation.continuation_fn
                if cont is not None:
                    next_text = cont(last_text)
                    if inspect.isawaitable(next_text):
                        next_text = await next_text
                    if next_text is None:
                        yield {"type": "error", "payload": {
                            "category": "agent_incomplete",
                            "kind": "no_complete_step_after_nudges",
                            "message": "agent did not call complete_step after nudges",
                            "detail": {"last_agent_text": last_text[:500]},
                        }}
                        return
                    user_msg = Message(role="user", content=(TextBlock(text=next_text),))
                    messages.append(user_msg)
                    transcript.append(user_msg)
                    elog.append_user_message(agent_id=aid, message=user_msg)
                    loop_err = guard.observe(TurnRecord(
                        assistant_text=last_text, tools=[],
                        has_complete_step=False))
                    if loop_err is not None:
                        yield perr.make_error_event(
                            loop_err, kind=getattr(loop_err, "kind", ""))
                        return
                    # Turn boundary (nudge path): the tool-less turn ended and the
                    # nudge user message was appended. Deliver any queued interject
                    # now — same as the tool-result turn boundary — so an interject
                    # during a nudge stall rides into the next model call instead of
                    # waiting an extra turn. No tool call means no subagent — S4 holds.
                    deliver_interject(invocation.coordinator, messages, transcript,
                                      elog, aid)
                    continue
                yield {"type": "error", "payload": {
                    "category": "agent_output_missing",
                    "kind": "no_complete_step",
                    "message": "agent finished without calling complete_step",
                    "detail": {"last_agent_text": last_text[:500]},
                }}
                return

            tool_results: list[ToolResultBlock] = []
            turn = TurnState(tool_calls_used=tool_calls_used)
            # §1: ALL `task` tus fan out concurrently as one batch at the first
            # task position; non-task tus stay strict sequential document-order.
            # The turn dispatcher yields one tool_heartbeat per processed tu and
            # any terminal event (yield it and stop); SubagentSuspended /
            # AwaitingUserInput propagate (terminal — no heartbeat).
            async for ev in dispatch_turn_tool_uses(
                    ctx=ctx, elog=elog, aid=aid, invocation=invocation,
                    tools=tools, tool_uses=tool_uses, tool_results=tool_results,
                    state=turn, ceiling=ceiling):
                yield ev
                if ev.get("type") != "tool_heartbeat":
                    return  # terminal event already yielded; stop the loop
            tool_calls_used = turn.tool_calls_used
            ceiling_tripped = turn.ceiling_tripped

            user_msg = Message(role="user", content=tuple(tool_results))
            messages.append(user_msg)
            transcript.append(user_msg)

            if ceiling_tripped:
                yield {"type": "error", "payload": {
                    "category": "config",
                    "kind": "tool_call_ceiling_exceeded",
                    "ceiling": ceiling,
                }}
                return

            by_id = {tu.id: tu for tu in tool_uses}
            record = TurnRecord(
                assistant_text="".join(
                    b.text for b in assistant_msg.content if isinstance(b, TextBlock)),
                tools=[
                    ToolOutcome(
                        name=by_id[b.tool_use_id].name if b.tool_use_id in by_id else "",
                        tool_use_id=b.tool_use_id,
                        input=by_id[b.tool_use_id].input if b.tool_use_id in by_id else {},
                        is_error=b.is_error,
                        result_preview=_result_preview(b.content))
                    for b in tool_results
                ],
                has_complete_step=False)
            loop_err = guard.observe(record)
            if loop_err is not None:
                yield perr.make_error_event(loop_err, kind=getattr(loop_err, "kind", ""))
                return

            if handoff_phase == "instruction_sent":
                # The agent's handoff-writing turn just completed (its write_file
                # tool_result is recorded). Signal the captured handoff and stop;
                # the engine stashes the transcript and reincarnates seeded with it.
                yield {"type": "handoff",
                       "payload": {"handoff_path": str(handoff_path)}}
                return
            if crossed_now:
                # Approaching the window: inject the agent-authored handoff
                # instruction as the next user turn (the same user-message append
                # interjects use), then capture the note on the following turn.
                instr = handoff_instruction(handoff_path)
                hmsg = Message(role="user", content=(TextBlock(text=instr),))
                messages.append(hmsg)
                transcript.append(hmsg)
                elog.append_user_message(agent_id=aid, message=hmsg)
                handoff_phase = "instruction_sent"
                continue

            # Turn boundary: this turn's tool_results are appended to
            # messages/transcript; deliver any queued human interject as a user
            # message before looping into the next model call. Reached only after
            # every tool (including an awaited `task` dispatch) has resolved, so a
            # subagent is never mid-dispatch here — guarantees S4.
            deliver_interject(invocation.coordinator, messages, transcript, elog, aid)

        # cancelled (interrupt)
        yield {"type": "error", "payload": {
            "category": "config",
            "kind": "interrupted",
            "message": "runner cancelled by interrupt",
        }}

    async def interrupt(self) -> None:
        from imp.agents.tools.bg import terminate_background_processes
        from imp.agents.tools.context import terminate_running_processes
        self._cancel = True
        # M5: the run loop's `finally` may have just set _cancel_scope to None and
        # exited the scope. Calling .cancel() on an already-exited anyio CancelScope
        # is a no-op (safe on the pinned anyio >=4,<5), and `_cancel` still stops
        # the loop on its next iteration — so the inverse race is benign.
        if self._cancel_scope is not None:
            self._cancel_scope.cancel()
        if self._ctx is not None:
            await terminate_running_processes(self._ctx)
            # Decision 7: pause kills background processes too (leak
            # resistance); the persisted tombstones give the resumed agent an
            # explanation. Idempotent vs the run() finally (both fire on pause).
            await terminate_background_processes(self._ctx)


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, model_name: str, *, client_factory: Any = None) -> None:
        self.model = model_name                     # public per Provider protocol
        self._client_factory = client_factory

    def create_runner(self, invocation: AgentInvocation) -> AgentRunner:
        return AnthropicAgentRunner(
            model_name=self.model, client_factory=self._client_factory,
            user_input_fn=invocation.user_input_fn,
        )


# Tier→model floor (spec 2026-06-08-config-file-split §2). Bare aliases track
# "latest" so the floor never goes stale; an operator can pin a dated model via
# any config layer.
TIER_DEFAULTS: dict[str, str] = {"low": "haiku", "medium": "sonnet", "high": "opus"}
ENV_VAR: str | None = "ANTHROPIC_API_KEY"


def _env_check() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _factory(model: str) -> Provider:
    return AnthropicProvider(model_name=model)
