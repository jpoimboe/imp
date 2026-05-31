"""OpenAI provider + runner (spec §6.10). Direct openai.AsyncOpenAI.

Loop:
  1. Build messages (transcript or initial_messages)
  2. POST to chat.completions.create with declared tools
  3. Append assistant response to transcript; append llm_response to EventLog
  4. For each tool_call block: dispatch tool, append tool_result to EventLog
  5. complete_step catches StepComplete → validate, emit agent_output, return
  6. No tool_calls in assistant and interactive: prompt user; else error

Yields ONLY control signals: agent_output, error.
All audit events (step_context, user_message, llm_response, tool_call,
tool_result, runner_warning, tool_killed) are written via EventLog.
"""

from __future__ import annotations

import inspect
import json
import os
from collections.abc import AsyncGenerator
from typing import Any, cast

from imp.agents.classify import ResumeState, classify_resume
from imp.agents.loop_guard import LoopGuard, ToolOutcome, TurnRecord
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
from imp.agents.subagents import dispatch_task, make_task_tool
from imp.agents.tools import Tool, get_registry
from imp.agents.tools.complete_step import StepComplete, make_complete_step_tool
from imp.agents.tools.context import ToolContext
from imp.agents.tools.errors import FatalToolError
from imp.agents.transcript import (
    Transcript,
    balance_success_on_resume,
    repair_dangling_tool_use_on_resume,
)
from imp.engine.schema import validate_agent_output
from imp.providers import errors as perr
from imp.providers._openai_schema import to_openai_strict
from imp.providers.base import Provider
from imp.providers.resolve import spawn_subagent_via_registry


def _classify_openai_exception(exc: BaseException) -> perr.ProviderError:
    """Translate an OpenAI SDK exception into the matching
    ProviderError subclass. Re-raises anything unrecognized so the
    runner's top-level handler can produce an 'unknown' event.
    """
    from openai import (
        APIConnectionError,
        APIStatusError,
        APITimeoutError,
        AuthenticationError,
        BadRequestError,
        InternalServerError,
        NotFoundError,
        PermissionDeniedError,
    )
    from openai import RateLimitError as _OpenAIRateLimit

    if isinstance(exc, AuthenticationError):
        return perr.AuthError(str(exc))
    if isinstance(exc, PermissionDeniedError):
        return perr.AuthError(str(exc))
    if isinstance(exc, _OpenAIRateLimit):
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
        detail: dict[str, object] = (
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
        if (
            "maximum context length" in msg_low
            or "context_length_exceeded" in msg_low
        ):
            return perr.ContextWindowError(msg)
        if "quota" in msg_low or "billing" in msg_low or "insufficient_quota" in msg_low:
            return perr.QuotaError(msg)
        return perr.ConfigError(msg)
    if isinstance(exc, APIStatusError):
        return perr.NetworkError(f"{exc.status_code}: {exc}")
    raise exc


def _build_tools(invocation: AgentInvocation) -> dict[str, Tool]:
    """Same shape as providers/anthropic.py::_build_tools — kept verbatim to
    preserve the "agent declares tools once, every runner sees the same set"
    invariant. Provider-specific shimming happens at the *schema* layer, not
    the *registry* layer."""
    reg = get_registry()
    out: dict[str, Tool] = {}
    for name in invocation.agent_def.tools:
        if name == "complete_step":
            continue
        if name == "task":
            continue
        if name not in reg:
            raise KeyError(f"agent declares unknown tool {name!r}")
        out[name] = reg[name]
    if "task" in invocation.agent_def.tools:
        out["task"] = make_task_tool()
    out["complete_step"] = make_complete_step_tool(invocation.agent_def.output_schema)
    return out


def _openai_tool_schema(t: Tool) -> dict[str, Any]:
    """Translate an imp Tool to OpenAI's function-call tool entry, with the
    strict-mode transform applied to the parameters."""
    return {
        "type": "function",
        "function": {
            "name": t.name,
            "description": t.description,
            "parameters": to_openai_strict(t.input_schema),
            "strict": True,
        },
    }


def _imp_to_openai_messages(messages: list[Message]) -> list[dict[str, Any]]:
    """Convert an imp message list to OpenAI's flattened message list.

    Assistant messages with multiple tool_use blocks → ONE OpenAI assistant
    message carrying all tool_calls.

    User messages with multiple tool_result blocks → MULTIPLE OpenAI
    `role: "tool"` messages, one per ToolResultBlock.
    """
    out: list[dict[str, Any]] = []
    for m in messages:
        if m.role == "system":
            out.append({"role": "system", "content": _join_text(m.content)})
            continue
        if m.role == "assistant":
            text = _join_text(m.content)
            tool_calls = []
            for b in m.content:
                if isinstance(b, ToolUseBlock):
                    tool_calls.append({
                        "id": b.id,
                        "type": "function",
                        "function": {
                            "name": b.name,
                            "arguments": json.dumps(b.input),
                        },
                    })
            entry: dict[str, Any] = {"role": "assistant"}
            # OpenAI accepts `content: null` only when `tool_calls` is
            # present; otherwise it 400s. Emit `""` when there's no text,
            # regardless of tool_calls presence — accepted in all cases and
            # avoids the latent bug a refusal-followed-by-continuation
            # scenario would otherwise trigger.
            entry["content"] = text if text else ""
            if tool_calls:
                entry["tool_calls"] = tool_calls
            out.append(entry)
            continue
        # user
        tool_results = [b for b in m.content if isinstance(b, ToolResultBlock)]
        if tool_results:
            for tr in tool_results:
                content = tr.content
                if tr.is_error:
                    # OpenAI doesn't have an is_error flag on tool messages;
                    # embed an explicit prefix so the model sees it.
                    content = f"[error] {content}"
                out.append({
                    "role": "tool",
                    "tool_call_id": tr.tool_use_id,
                    "content": content,
                })
            continue
        # Plain user text (initial prompt, or interactive follow-up)
        out.append({"role": "user", "content": _join_text(m.content)})
    return out


def _join_text(content: tuple[Any, ...]) -> str:
    """Concatenate TextBlock.text values; ignore other block types."""
    return "".join(b.text for b in content if isinstance(b, TextBlock))


def _openai_message_to_imp(
    msg: Any,
) -> tuple[Message, list[str]]:
    """Convert an OpenAI assistant message (SDK object form) to an imp Message
    + warnings list.

    Called from tests only; the production runner now reconstructs the assistant
    Message from the serialized llm_response.raw via _parse_openai_message. This
    helper is retained as a tested record of the SDK-object → imp Message surface.

    Unknown shapes (e.g., msg.refusal — OpenAI's refusal field) become
    runner_warning entries, never raise (unknown content block types translate
    to a runner_warning rather than crashing the runner)."""
    blocks: list[Any] = []
    warnings: list[str] = []

    text = getattr(msg, "content", None)
    if text:
        blocks.append(TextBlock(text=text))

    tool_calls = getattr(msg, "tool_calls", None) or []
    for tc in tool_calls:
        tc_type = getattr(tc, "type", "function")
        if tc_type != "function":
            warnings.append(f"skipped tool_call type {tc_type!r}")
            continue
        fn = getattr(tc, "function", None)
        if fn is None:
            warnings.append("tool_call missing function block")
            continue
        try:
            args = json.loads(fn.arguments) if fn.arguments else {}
        except json.JSONDecodeError as e:
            warnings.append(f"tool_call {tc.id!r} arguments not JSON: {e}")
            args = {}
        blocks.append(ToolUseBlock(id=tc.id, name=fn.name, input=args))

    refusal = getattr(msg, "refusal", None)
    if refusal:
        warnings.append(f"skipped refusal: {refusal[:80]!r}")

    return Message(role="assistant", content=tuple(blocks)), warnings


def _serialize_openai_response(response: Any) -> dict[str, Any]:
    """response → llm_response.raw dict. SDK .model_dump() preferred; fake
    fallback introspects choices[0].message + usage."""
    dump = getattr(response, "model_dump", None)
    if callable(dump):
        try:
            return cast(dict[str, Any], dump(mode="json"))
        except TypeError:
            return cast(dict[str, Any], dump())
    choice = response.choices[0]
    msg = choice.message
    tool_calls = []
    for tc in getattr(msg, "tool_calls", None) or []:
        fn = getattr(tc, "function", None)
        tool_calls.append({
            "id": getattr(tc, "id", None),
            "type": getattr(tc, "type", "function"),
            "function": {"name": getattr(fn, "name", None),
                         "arguments": getattr(fn, "arguments", "")},
        })
    return {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": getattr(msg, "content", None),
                "tool_calls": tool_calls or None,
                "refusal": getattr(msg, "refusal", None),
            },
            "finish_reason": getattr(choice, "finish_reason", None),
        }],
        "usage": _usage_dict(getattr(response, "usage", None)),
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
            ("prompt_tokens", "completion_tokens", "total_tokens")
            if getattr(usage, k, None) is not None}


def _parse_openai_message(raw: dict[str, Any]) -> Message:
    msg = raw["choices"][0]["message"]
    blocks: list[Any] = []
    text = msg.get("content")
    if text:
        blocks.append(TextBlock(text=text))
    for tc in msg.get("tool_calls") or []:
        if tc.get("type", "function") != "function":
            continue
        fn = tc.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {}
        blocks.append(ToolUseBlock(id=tc["id"], name=cast(str, fn.get("name")), input=args))
    return Message(role="assistant", content=tuple(blocks))


def _extract_openai_diagnostics(raw: dict[str, Any]) -> Diagnostics:
    msg = raw["choices"][0]["message"]
    warnings: list[str] = []
    for tc in msg.get("tool_calls") or []:
        if tc.get("type", "function") != "function":
            warnings.append(f"skipped tool_call type {tc.get('type')!r}")
        else:
            fn = tc.get("function") or {}
            try:
                json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError as e:
                warnings.append(f"tool_call {tc.get('id')!r} arguments not JSON: {e}")
    refusal = msg.get("refusal")
    if refusal:
        warnings.append(f"skipped refusal: {refusal[:80]!r}")
    return Diagnostics(block_warnings=tuple(warnings), thinkings=())


class OpenAIAgentRunner:
    """Mirrors AnthropicAgentRunner.run; differences live in the translation
    helpers above and in the API call below."""

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
        self._ctx: ToolContext | None = None

    def _get_client(self) -> Any:
        if self._client is None:
            if self._client_factory is not None:
                self._client = self._client_factory()
            else:
                import openai
                self._client = openai.AsyncOpenAI()
        return self._client

    async def _close_client(self) -> None:
        """Close the underlying SDK client if we own it and it supports close().

        Real `openai.AsyncOpenAI` owns an httpx pool; without explicit close()
        the pool's async teardown can race with pytest-anyio's per-test loop
        teardown and surface "Event loop is closed" errors. Test fakes don't
        expose `.close()` — skip safely.
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
            except Exception as exc:
                try:
                    classified = _classify_openai_exception(exc)
                except Exception:
                    yield perr.make_unknown_error_event(exc)
                    return
                yield perr.make_error_event(classified)
                return
        finally:
            await self._close_client()

    async def _run_loop(
        self,
        invocation: AgentInvocation,
    ) -> AsyncGenerator[Event, None]:
        from imp.engine.state import JsonlEventLog
        elog = invocation.event_log or JsonlEventLog(invocation.events_path)
        aid = invocation.agent_id or "step:unnamed"

        transcript = Transcript(invocation.transcript_dir / "transcript.jsonl")
        tools = _build_tools(invocation)
        ctx = ToolContext(
            project_root=invocation.change_context.project_root,
            artifacts_dir=invocation.change_context.artifacts_dir,
            fs_permissions=invocation.fs_permissions,
            events_path=invocation.events_path,
            subagent_defs=invocation.subagent_defs,
            provider_name=invocation.provider_name,
            model_name=invocation.model_name,
            parent_transcript_dir=invocation.transcript_dir,
            interactive=invocation.interactive,
        )
        ctx.change_context = invocation.change_context
        ctx.spawn_subagent = spawn_subagent_via_registry
        ctx.resolved_roles = invocation.resolved_roles
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
                repair = repair_dangling_tool_use_on_resume(messages, transcript)
                if repair is not None:
                    for b in repair.content:
                        if isinstance(b, ToolResultBlock):
                            elog.append_tool_result(agent_id=aid, block=b)
            state = classify_resume(messages, interactive=invocation.interactive)
            if state is ResumeState.DANGLING_TOOL_USE:
                yield {"type": "error", "payload": {
                    "category": "config",
                    "kind": "transcript_invalid",
                    "message": "transcript ended with dangling tool_use",
                }}
                return
            if state is ResumeState.MISSING_COMPLETE_STEP:
                last_text = ""
                if messages and messages[-1].role == "assistant":
                    last_text = "".join(
                        b.text for b in messages[-1].content
                        if isinstance(b, TextBlock)
                    )
                yield {"type": "error", "payload": {
                    "category": "agent_output_missing",
                    "kind": "missing_complete_step",
                    "message": "autonomous agent transcript ended without complete_step",
                    "detail": {"last_agent_text": last_text[:500]},
                }}
                return
            if state is ResumeState.PROMPT_USER:
                if ctx.user_input_fn is None:
                    yield {"type": "error", "payload": {
                        "category": "config",
                        "kind": "no_user_input_fn",
                        "message": (
                            "PROMPT_USER resume requires user_input_fn; "
                            "none installed by runner"
                        ),
                    }}
                    return
                user_text = ctx.user_input_fn("")
                if inspect.isawaitable(user_text):
                    user_text = await user_text
                user_msg = Message(
                    role="user", content=(TextBlock(text=user_text),),
                )
                messages.append(user_msg)
                transcript.append(user_msg)
                elog.append_user_message(agent_id=aid, message=user_msg)
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
                tools_schema=[_openai_tool_schema(t) for t in tools.values()],
                params={},
                max_tool_calls=invocation.max_tool_calls)
            for m in invocation.initial_messages:
                if all(isinstance(b, TextBlock) for b in m.content):
                    elog.append_user_message(agent_id=aid, message=m)

        ceiling = invocation.max_tool_calls  # may be None (no cap)
        openai_tools = [_openai_tool_schema(t) for t in tools.values()]
        # OpenAI takes the system prompt as the first message rather than a
        # separate parameter. Build the prefix once; reuse on each call.
        system_message: dict[str, Any] = {"role": "system", "content": invocation.system_prompt}
        # Stable routing/affinity hint for OpenAI's automatic prompt cache.
        # Keyed to one growing conversation (one agent invocation within one
        # change) so the prefix stays cache-resident turn-over-turn. change_id
        # is required: agent_id alone is change-agnostic, so concurrent changes
        # running the same step would collide on one routing slot.
        prompt_cache_key = f"{invocation.change_context.change_id}:{aid}"
        guard = LoopGuard()

        while not self._cancel:
            client = self._get_client()
            response = await client.chat.completions.create(
                model=self.model,
                messages=[system_message, *_imp_to_openai_messages(messages)],
                tools=openai_tools,
                prompt_cache_key=prompt_cache_key,
            )

            raw = _serialize_openai_response(response)
            assistant_msg = _parse_openai_message(raw)
            diag = _extract_openai_diagnostics(raw)
            for w in diag.block_warnings:
                elog.append(agent_id=aid, type="runner_warning",
                            payload={"message": w})
            messages.append(assistant_msg)
            transcript.append(assistant_msg)
            elog.append_llm_response(
                agent_id=aid, provider=invocation.provider_name, model=self.model,
                raw=raw, usage=raw.get("usage") or {},
                stop_reason=raw["choices"][0].get("finish_reason"))

            tool_uses = [b for b in assistant_msg.content if isinstance(b, ToolUseBlock)]
            if not tool_uses:
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
                        assistant_text=last_text, tools=[], has_complete_step=False))
                    if loop_err is not None:
                        yield perr.make_error_event(
                            loop_err, kind=getattr(loop_err, "kind", ""))
                        return
                    continue
                # Legacy/test fallback: no continuation_fn wired by the executor.
                if invocation.interactive and ctx.user_input_fn is not None:
                    user_text = ctx.user_input_fn("")
                    if inspect.isawaitable(user_text):
                        user_text = await user_text
                    user_msg = Message(role="user", content=(TextBlock(text=user_text),))
                    messages.append(user_msg)
                    transcript.append(user_msg)
                    elog.append_user_message(agent_id=aid, message=user_msg)
                    continue
                yield {"type": "error", "payload": {
                    "category": "agent_output_missing",
                    "kind": "no_complete_step",
                    "message": "agent finished without calling complete_step",
                    "detail": {"last_agent_text": last_text[:500]},
                }}
                return

            tool_results: list[ToolResultBlock] = []
            ceiling_tripped = False
            for tu in tool_uses:
                if ceiling is not None and tool_calls_used >= ceiling:
                    # Backfill error tool_results to preserve transcript invariant.
                    err_text = f"tool_call_ceiling_exceeded (ceiling={ceiling})"
                    block = ToolResultBlock(tu.id, err_text, is_error=True)
                    tool_results.append(block)
                    elog.append_tool_result(agent_id=aid, block=block)
                    ceiling_tripped = True
                    continue
                tool_calls_used += 1

                tc_ev = elog.append_tool_call(agent_id=aid, block=tu)

                # task special-case: bypasses the sync Tool.invoke path
                # entirely — go straight to the async dispatcher.
                # Use the persisted tool_call event id for durable parent link.
                if tu.name == "task":
                    try:
                        result_str = await dispatch_task(
                            ctx, tu.input["subagent_name"], tu.input["task_description"],
                            tool_call_event_id=tc_ev["id"],
                        )
                    except Exception as e:
                        err_text = f"{type(e).__name__}: {e}"
                        block = ToolResultBlock(tu.id, err_text, is_error=True)
                        tool_results.append(block)
                        elog.append_tool_result(agent_id=aid, block=block)
                        continue
                    block = ToolResultBlock(tu.id, result_str)
                    tool_results.append(block)
                    elog.append_tool_result(agent_id=aid, block=block)
                    continue

                tool = tools.get(tu.name)
                if tool is None:
                    msg = f"unknown tool {tu.name!r}"
                    block = ToolResultBlock(tu.id, msg, is_error=True)
                    tool_results.append(block)
                    elog.append_tool_result(agent_id=aid, block=block)
                    continue
                try:
                    raw_result = await tool.async_invoke(ctx, tu.input)
                except StepComplete as sc:
                    try:
                        validate_agent_output(sc.args_payload, invocation.agent_def.output_schema)
                    except Exception as ve:
                        yield {"type": "error", "payload": {
                            "category": "agent_output_missing",
                            "kind": "output_validation",
                            "message": str(ve),
                        }}
                        return
                    yield {"type": "agent_output", "payload": sc.args_payload}
                    return
                except FatalToolError as fte:
                    yield {"type": "error", "payload": {
                        "category": "config",
                        "kind": "fatal_tool_error",
                        "tool": tu.name,
                        "message": str(fte),
                    }}
                    return
                except Exception as e:
                    err_text = f"{type(e).__name__}: {e}"
                    block = ToolResultBlock(tu.id, err_text, is_error=True)
                    tool_results.append(block)
                    elog.append_tool_result(agent_id=aid, block=block)
                    continue

                content_str = (raw_result if isinstance(raw_result, str)
                               else json.dumps(raw_result, default=str))
                if tu.name == "bash":
                    # Detect a kill so the log carries a tool_killed event.
                    try:
                        parsed = json.loads(content_str)
                    except (TypeError, ValueError):
                        parsed = {}
                    rc = parsed.get("returncode")
                    if isinstance(rc, int) and rc < 0:
                        elog.append(agent_id=aid, type="tool_killed",
                                    payload={"id": tu.id, "name": "bash",
                                             "signal": -rc})
                block = ToolResultBlock(tu.id, content_str)
                tool_results.append(block)
                elog.append_tool_result(agent_id=aid, block=block)

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
                        is_error=b.is_error)
                    for b in tool_results
                ],
                has_complete_step=False)
            loop_err = guard.observe(record)
            if loop_err is not None:
                yield perr.make_error_event(loop_err, kind=getattr(loop_err, "kind", ""))
                return

        # cancelled (interrupt)
        yield {"type": "error", "payload": {
            "category": "config",
            "kind": "interrupted",
            "message": "runner cancelled by interrupt",
        }}

    async def interrupt(self) -> None:
        from imp.agents.tools.context import terminate_running_processes
        self._cancel = True
        if self._ctx is not None:
            await terminate_running_processes(self._ctx, timeout=2.0)


class OpenAIProvider:
    name = "openai"

    def __init__(self, model_name: str, *, client_factory: Any = None) -> None:
        self.model = model_name                     # public per Provider protocol
        self._client_factory = client_factory

    def create_runner(self, invocation: AgentInvocation) -> AgentRunner:
        return OpenAIAgentRunner(
            model_name=self.model, client_factory=self._client_factory,
            user_input_fn=invocation.user_input_fn,
        )


def _env_check() -> bool:
    return bool(os.environ.get("OPENAI_API_KEY"))


def _factory(model: str) -> Provider:
    return OpenAIProvider(model_name=model)
