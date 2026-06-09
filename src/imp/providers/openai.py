"""OpenAI provider + runner (spec §6.10). Direct openai.AsyncOpenAI.

Loop:
  1. Build messages (transcript or initial_messages)
  2. POST to responses.create (store=False) with declared tools
  3. Append assistant response to transcript; append llm_response to EventLog
  4. For each tool_call block: dispatch tool, append tool_result to EventLog
  5. complete_step catches StepComplete → validate, emit agent_output, return
  6. No tool_calls in assistant and interactive: prompt user; else error

Yields ONLY control signals: agent_output, error.
All audit events (step_context, user_message, llm_response, tool_call,
tool_result, runner_warning, tool_killed) are written via EventLog.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
from collections.abc import AsyncGenerator
from typing import Any, cast

import anyio

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
from imp.agents.subagents import dispatch_task, make_task_tool, raise_if_suspended
from imp.agents.tools import Tool, get_registry
from imp.agents.tools.complete_step import StepComplete, make_complete_step_tool
from imp.agents.tools.context import ToolContext
from imp.agents.tools.errors import FatalToolError
from imp.agents.tools.load_skill import make_load_skill_tool
from imp.agents.tools.wait_for_user import make_wait_for_user_tool
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
from imp.providers.web_native import (
    WEB_TOOL_NAMES,
    check_web_capability,
    openai_web_tool_specs,
)


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
    check_web_capability(invocation.provider_name, invocation.agent_def)
    for name in invocation.agent_def.tools:
        if name == "complete_step":
            continue
        if name == "task":
            continue
        if name in WEB_TOOL_NAMES:
            # Reserved native-capability names (§4): never registry-looked-up;
            # routed to the native-injection path in _run_loop.
            continue
        if name == "load_skill":
            # Auto-injected below; tolerate a redundant explicit declaration.
            continue
        if name == "wait_for_user":
            # Synthesized below when declared; tolerate the explicit declaration.
            continue
        if name not in reg:
            raise KeyError(f"agent declares unknown tool {name!r}")
        out[name] = reg[name]
    if "task" in invocation.agent_def.tools:
        out["task"] = make_task_tool(invocation.tiers)
    out["complete_step"] = make_complete_step_tool(invocation.agent_def.output_schema)
    out["load_skill"] = make_load_skill_tool()
    if "wait_for_user" in invocation.agent_def.tools:
        out["wait_for_user"] = make_wait_for_user_tool()
    return out


def _responses_tool_schema(t: Tool) -> dict[str, Any]:
    """Responses function-tool entry: flat shape (no nested `function` wrapper),
    strict mode on. Replaces _openai_tool_schema at the Task 17 flip."""
    return {
        "type": "function",
        "name": t.name,
        "description": t.description,
        "parameters": to_openai_strict(t.input_schema),
        "strict": True,
    }


def _imp_to_responses_input(messages: list[Message]) -> list[dict[str, Any]]:
    """Convert imp messages to Responses `input[]` items.

    - assistant text  → {type:message, role:assistant, content:[output_text]}
    - assistant tool_use → {type:function_call, call_id, name, arguments}
    - user tool_result → {type:function_call_output, call_id, output}
    - plain user/system text → {type:message, role, content:[input_text]}

    The system prompt is passed separately via the `instructions` param (see
    the call site), so `system` messages are skipped here.
    """
    out: list[dict[str, Any]] = []
    for m in messages:
        if m.role == "system":
            continue  # carried via `instructions`
        if m.role == "assistant":
            text = _join_text(m.content)
            if text:
                out.append({"type": "message", "role": "assistant",
                            "content": [{"type": "output_text", "text": text}]})
            for b in m.content:
                if isinstance(b, ToolUseBlock):
                    out.append({"type": "function_call", "call_id": b.id,
                                "name": b.name, "arguments": json.dumps(b.input)})
            continue
        # user
        tool_results = [b for b in m.content if isinstance(b, ToolResultBlock)]
        if tool_results:
            for tr in tool_results:
                output = tr.content
                if tr.is_error:
                    output = f"[error] {output}"
                out.append({"type": "function_call_output",
                            "call_id": tr.tool_use_id, "output": output})
            continue
        out.append({"type": "message", "role": "user",
                    "content": [{"type": "input_text", "text": _join_text(m.content)}]})
    return out


def _join_text(content: tuple[Any, ...]) -> str:
    """Concatenate TextBlock.text values; ignore other block types."""
    return "".join(b.text for b in content if isinstance(b, TextBlock))


def _serialize_responses_response(response: Any) -> dict[str, Any]:
    """response → llm_response.raw dict. SDK .model_dump() preferred; the fake
    path returns {output, usage} directly. Replaces _serialize_openai_response
    at the Task 17 flip."""
    dump = getattr(response, "model_dump", None)
    if callable(dump):
        try:
            return cast(dict[str, Any], dump(mode="json"))
        except TypeError:
            return cast(dict[str, Any], dump())
    # Minimal fallback for objects exposing `.output` / `.usage`.
    return {
        "output": getattr(response, "output", []) or [],
        "usage": _usage_dict(getattr(response, "usage", None)),
    }


def _parse_responses_message(raw: dict[str, Any]) -> Message:
    """Reconstruct the assistant Message from Responses `output[]`.

    message → output_text parts become TextBlocks; function_call items become
    ToolUseBlocks. web_search_call / reasoning items are NOT conversation
    content (web events are emitted separately in Task 18)."""
    blocks: list[Any] = []
    for item in raw.get("output", []) or []:
        itype = item.get("type")
        if itype == "message":
            text = "".join(
                part.get("text", "")
                for part in item.get("content", []) or []
                if part.get("type") == "output_text")
            if text:
                blocks.append(TextBlock(text=text))
        elif itype == "function_call":
            try:
                args = json.loads(item.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            blocks.append(ToolUseBlock(
                id=item.get("call_id", ""),
                name=cast(str, item.get("name")), input=args))
    return Message(role="assistant", content=tuple(blocks))


def _usage_dict(usage: Any) -> dict[str, Any]:
    if usage is None:
        return {}
    dump = getattr(usage, "model_dump", None)
    if callable(dump):
        return cast(dict[str, Any], dump())
    if isinstance(usage, dict):
        return usage
    return {k: getattr(usage, k) for k in
            ("input_tokens", "output_tokens", "total_tokens")
            if getattr(usage, k, None) is not None}


def _extract_responses_diagnostics(raw: dict[str, Any]) -> Diagnostics:
    """Responses-vocabulary diagnostics. Replaces _extract_openai_diagnostics
    at the Task 17 flip."""
    warnings: list[str] = []
    for item in raw.get("output", []) or []:
        itype = item.get("type")
        if itype == "function_call":
            try:
                json.loads(item.get("arguments") or "{}")
            except json.JSONDecodeError as e:
                warnings.append(f"tool_call {item.get('call_id')!r} arguments not JSON: {e}")
        elif itype == "refusal":
            warnings.append(f"skipped refusal: {str(item.get('refusal'))[:80]!r}")
        # message / function_call (ok) / web_search_call / reasoning: no warning
    return Diagnostics(block_warnings=tuple(warnings), thinkings=())


def _collect_openai_url_citations(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """url_citation annotations across all message output_text parts →
    [{url, title?}] (best-effort; only url guaranteed)."""
    cites: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw.get("output", []) or []:
        if item.get("type") != "message":
            continue
        for part in item.get("content", []) or []:
            if part.get("type") != "output_text":
                continue
            for ann in part.get("annotations", []) or []:
                if ann.get("type") != "url_citation":
                    continue
                url = ann.get("url")
                if not url or url in seen:
                    continue
                seen.add(url)
                c: dict[str, Any] = {"url": url}
                if ann.get("title"):
                    c["title"] = ann["title"]
                cites.append(c)
    return cites


def _emit_openai_web_events(raw: dict[str, Any], elog: Any, aid: str) -> None:
    """Normalized web events from Responses output. Each web_search_call →
    a web_search tool_call.

    Citation attribution caveat: url_citation annotations live on the assistant
    output_text, not linked to a specific web_search_call id, so with >1 search
    in a turn every search's tool_result carries the full citation list (no
    per-search attribution is possible from this API shape). Single-search is
    exact.
    """
    web_calls = [it for it in (raw.get("output") or [])
                 if it.get("type") == "web_search_call"]
    if not web_calls:
        return
    citations = _collect_openai_url_citations(raw)
    for it in web_calls:
        action = it.get("action") or {}
        query = action.get("query", "")
        call_id = it.get("id", "")
        elog.append_tool_call(
            agent_id=aid,
            block=ToolUseBlock(id=call_id, name="web_search",
                               input={"query": query}))
        elog.append_tool_result(
            agent_id=aid,
            block=ToolResultBlock(call_id, "\n".join(
                (f"{c['title']} — {c['url']}" if c.get("title") else c["url"])
                for c in citations)),
            citations=citations)


def _openai_stop_reason(raw: dict[str, Any]) -> str | None:
    """Best-effort terminal marker for the llm_response event. Responses exposes
    `status` at the top level; the fake omits it → None, which is fine."""
    return raw.get("status")


_CACHE_KEY_MAX = 64        # OpenAI's hard limit on prompt_cache_key
_CACHE_KEY_HASH_LEN = 8    # hex chars of disambiguating digest


def _prompt_cache_key(change_id: str, aid: str) -> str:
    """OpenAI routing/affinity hint, bounded to the 64-char API limit.

    Stable per (change, agent) invocation; distinct per change and per
    agent. Keys that fit are passed through verbatim. Over-long keys keep
    the whole aid (the within-change discriminator one greps for) and the
    change_id head (the cross-change discriminator), then append a hash of the
    *full* change_id:aid so distinctness on both survives truncation.
    """
    full = f"{change_id}:{aid}"
    if len(full) <= _CACHE_KEY_MAX:
        return full
    digest = hashlib.sha256(full.encode()).hexdigest()[:_CACHE_KEY_HASH_LEN]
    budget = _CACHE_KEY_MAX - _CACHE_KEY_HASH_LEN - 1   # 55: readable, before "-{digest}"
    aid_keep = min(len(aid), budget - 1)                # keep >=1 char for id + ":"
    id_keep = budget - aid_keep - 1                     # -1 for the ":" separator
    return f"{change_id[:id_keep]}:{aid[-aid_keep:]}-{digest}"


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
        self._cancel_scope: Any = None
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
                raise_if_suspended(exc)
                if self._cancel:
                    yield {"type": "error", "payload": {
                        "category": "config",
                        "kind": "interrupted",
                        "message": "runner cancelled by interrupt",
                    }}
                    return
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
            fs_permissions=invocation.fs_permissions,
            events_path=invocation.events_path,
            subagent_defs=invocation.subagent_defs,
            provider_name=invocation.provider_name,
            model_name=invocation.model_name,
            parent_transcript_dir=invocation.transcript_dir,
            interactive=invocation.interactive,
            agent_id=aid,
        )
        ctx.change_context = invocation.change_context
        ctx.change_state = invocation.change_state
        ctx.change = invocation.change          # live Change for rename_change
        ctx.spawn_subagent = spawn_subagent_via_registry
        ctx.resolved_tiers = invocation.resolved_tiers
        ctx.subagent_depth = invocation.subagent_depth
        ctx.coordinator = invocation.coordinator
        ctx.tiers = invocation.tiers
        ctx.lint_command = invocation.lint_command
        ctx.test_command = invocation.test_command
        ctx.non_interactive = invocation.non_interactive
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
                tools_schema=[_responses_tool_schema(t) for t in tools.values()],
                params={"store": False},
                max_tool_calls=invocation.max_tool_calls)
            for m in invocation.initial_messages:
                if all(isinstance(b, TextBlock) for b in m.content):
                    elog.append_user_message(agent_id=aid, message=m)

        ceiling = invocation.max_tool_calls  # may be None (no cap)
        openai_tools = [_responses_tool_schema(t) for t in tools.values()]
        openai_tools.extend(openai_web_tool_specs(invocation.agent_def))
        # Stable routing/affinity hint for OpenAI's automatic prompt cache.
        # Keyed to one growing conversation (one agent invocation within one
        # change) so the prefix stays cache-resident turn-over-turn. id
        # is required: agent_id alone is change-agnostic, so concurrent changes
        # running the same step would collide on one routing slot.
        # Keyed to the IMMUTABLE id, not the slug: a slug rotates at the
        # spec->plan title rename and would bust the cache mid-conversation
        # (spec §9). [AC 10]
        prompt_cache_key = _prompt_cache_key(invocation.change_context.id, aid)
        guard = LoopGuard()

        while not self._cancel:
            client = self._get_client()
            # N4 race note: an interrupt() between the loop-head _cancel check and
            # this assignment leaves this one request uncancelled (loop exits next
            # iteration via _cancel). One extra in-flight request, worst case.
            self._cancel_scope = anyio.CancelScope()
            response = None
            try:
                with self._cancel_scope:
                    response = await client.responses.create(
                        model=self.model,
                        instructions=invocation.system_prompt,
                        input=_imp_to_responses_input(messages),
                        tools=openai_tools,
                        store=False,
                        prompt_cache_key=prompt_cache_key,
                    )
            finally:
                # N3: reset even on a genuine API error so a later interrupt()
                # never cancels a stale exited scope.
                self._cancel_scope = None
            if response is None:
                # interrupt() cancelled the in-flight request — exit the loop and
                # fall through to the "cancelled (interrupt)" yield below.
                break

            raw = _serialize_responses_response(response)
            assistant_msg = _parse_responses_message(raw)
            diag = _extract_responses_diagnostics(raw)
            for w in diag.block_warnings:
                elog.append(agent_id=aid, type="runner_warning",
                            payload={"message": w})
            messages.append(assistant_msg)
            transcript.append(assistant_msg)
            elog.append_llm_response(
                agent_id=aid, provider=invocation.provider_name, model=self.model,
                raw=raw, usage=raw.get("usage") or {},
                stop_reason=_openai_stop_reason(raw))
            _emit_openai_web_events(raw, elog, aid)

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
                    if invocation.interactive:
                        # Human handoff: this text-only turn awaits fresh human
                        # input, so it is not an autonomous loop iteration —
                        # reset so normal multi-turn conversation never trips
                        # no_progress. (The nudge path below still observes.)
                        guard.note_external_input()
                    else:
                        loop_err = guard.observe(TurnRecord(
                            assistant_text=last_text, tools=[],
                            has_complete_step=False))
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
                            tier=tu.input.get("tier"),
                        )
                    except Exception as e:
                        raise_if_suspended(e)
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
        # M5: the run loop's `finally` may have just set _cancel_scope to None and
        # exited the scope. Calling .cancel() on an already-exited anyio CancelScope
        # is a no-op (safe on the pinned anyio >=4,<5), and `_cancel` still stops
        # the loop on its next iteration — so the inverse race is benign.
        if self._cancel_scope is not None:
            self._cancel_scope.cancel()
        if self._ctx is not None:
            await terminate_running_processes(self._ctx)


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


TIER_DEFAULTS: dict[str, str] = {
    "low": "gpt-5.4-mini", "medium": "gpt-5.3-codex", "high": "gpt-5.5",
}
ENV_VAR: str | None = "OPENAI_API_KEY"


def _env_check() -> bool:
    return bool(os.environ.get("OPENAI_API_KEY"))


def _factory(model: str) -> Provider:
    return OpenAIProvider(model_name=model)
