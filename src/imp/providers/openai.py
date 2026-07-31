"""OpenAI provider + runner (spec §6.10). Direct openai.AsyncOpenAI.

Loop:
  1. Build messages (transcript or initial_messages)
  2. Stream via responses.stream (store=False) with declared tools; pulse
     coordinator.note_activity() per streamed event for the inactivity watchdog
  3. Append assistant response to transcript; append agent_message to EventLog
  4. For each tool_call block: dispatch tool, append tool_result to EventLog
  5. complete_step catches StepComplete → validate, emit agent_result, return
  6. No tool_calls in assistant: nudge via continuation_fn; else error

Yields ONLY control signals: agent_result, error.
All audit events (step_context, user_message, agent_message, tool_call,
tool_result, runner_warning, tool_killed) are written via EventLog.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import time
from collections.abc import AsyncGenerator
from typing import Any, cast

import anyio

from imp.agents.agent_loop import run_agent_loop
from imp.agents.runner import (
    AgentInvocation,
    AgentRunner,
    ContentPart,
    Diagnostics,
    Event,
    Message,
    TextBlock,
    TextPart,
    ToolResultBlock,
    ToolUseBlock,
)
from imp.agents.subagents import make_task_tool, raise_if_suspended
from imp.agents.tools import Tool, get_registry
from imp.agents.tools.complete_step import make_complete_step_tool
from imp.agents.tools.context import (
    ToolContext,
    request_bracketed,
)
from imp.agents.tools.load_skill import make_load_skill_tool
from imp.engine.awaiting_user_input import AwaitingUserInput
from imp.providers import errors as perr
from imp.providers._openai_schema import to_openai_strict
from imp.providers.base import Provider
from imp.providers.message_parsers import _parse_responses_message
from imp.providers.push_session import PushSession
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
        if name == "sub_agent":
            continue
        if name in WEB_TOOL_NAMES:
            # Reserved native-capability names (§4): never registry-looked-up;
            # injected via OpenAISession._web_tool_specs().
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
        # Auto-granted with bash (precedent: load_skill injection):
        # run_in_background is a bash PARAMETER, so an agent with bash but no
        # read/kill tool is a trap — a backgrounded process it can never see
        # or stop. The capability set must be atomic.
        out["bash_output"] = reg["bash_output"]
        out["bash_kill"] = reg["bash_kill"]
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


def _content_parts_to_responses(parts: list[ContentPart]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for p in parts:
        if isinstance(p, TextPart):
            items.append({"type": "input_text", "text": p.text})
        else:
            items.append({"type": "input_image",
                          "image_url": f"data:{p.media_type};base64,{p.data}",
                          "detail": "auto"})
    return items


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
                # Plain function_call / function_call_output replay — no encrypted
                # reasoning items threaded back — is accepted by the Responses API
                # (live-verified, GPT-5 family). Do NOT add
                # include=["reasoning.encrypted_content"] or echo raw reasoning
                # output items into input[] (the latter 400s). This was the #1
                # risk of the Responses migration.
                if isinstance(b, ToolUseBlock):
                    out.append({"type": "function_call", "call_id": b.id,
                                "name": b.name, "arguments": json.dumps(b.input)})
            continue
        # user
        tool_results = [b for b in m.content if isinstance(b, ToolResultBlock)]
        if tool_results:
            for tr in tool_results:
                if isinstance(tr.content, list):
                    out.append({"type": "function_call_output",
                                "call_id": tr.tool_use_id,
                                "output": _content_parts_to_responses(tr.content)})  # type: ignore[arg-type]
                    continue
                output = tr.content
                if tr.is_error:
                    output = f"[error] {output}"
                out.append({"type": "function_call_output",
                            "call_id": tr.tool_use_id, "output": output})
            # Co-riding text (resume feedback / durability note) must survive:
            # emit it as a follow-on user message so it reaches the model. The
            # anthropic/claude_code builders render this mixed content natively.
            text = _join_text(m.content)
            if text:
                out.append({"type": "message", "role": "user",
                            "content": [{"type": "input_text", "text": text}]})
            continue
        out.append({"type": "message", "role": "user",
                    "content": [{"type": "input_text", "text": _join_text(m.content)}]})
    return out


def _join_text(content: tuple[Any, ...]) -> str:
    """Concatenate TextBlock.text values; ignore other block types."""
    return "".join(b.text for b in content if isinstance(b, TextBlock))


def _serialize_responses_response(response: Any) -> dict[str, Any]:
    """response → agent_message.raw dict. SDK .model_dump() preferred; the fake
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
    """Best-effort terminal marker for the agent_message event. Responses exposes
    `status` at the top level; the fake omits it → None, which is fine."""
    return raw.get("status")


_CACHE_KEY_MAX = 64        # OpenAI's hard limit on prompt_cache_key
_CACHE_KEY_HASH_LEN = 8    # hex chars of disambiguating digest


def _prompt_cache_key(quest_id: str, aid: str) -> str:
    """OpenAI routing/affinity hint, bounded to the 64-char API limit.

    Stable per (quest, agent) invocation; distinct per quest and per
    agent. Keys that fit are passed through verbatim. Over-long keys keep
    the whole aid (the within-quest discriminator one greps for) and the
    quest_id head (the cross-quest discriminator), then append a hash of the
    *full* quest_id:aid so distinctness on both survives truncation.
    """
    full = f"{quest_id}:{aid}"
    if len(full) <= _CACHE_KEY_MAX:
        return full
    digest = hashlib.sha256(full.encode()).hexdigest()[:_CACHE_KEY_HASH_LEN]
    budget = _CACHE_KEY_MAX - _CACHE_KEY_HASH_LEN - 1   # 55: readable, before "-{digest}"
    aid_keep = min(len(aid), budget - 1)                # keep >=1 char for id + ":"
    id_keep = budget - aid_keep - 1                     # -1 for the ":" separator
    return f"{quest_id[:id_keep]}:{aid[-aid_keep:]}-{digest}"


class OpenAISession(PushSession):
    def _build_tools(self) -> dict[str, Any]:
        return _build_tools(self.invocation)

    def _api_tool_schemas(self) -> list[Any]:
        return [_responses_tool_schema(t) for t in self.tools.values()]

    def _web_tool_specs(self) -> list[Any]:
        return list(openai_web_tool_specs(self.invocation.agent_def))

    def _step_context_model(self) -> str:
        return cast(str, self.runner.model)

    def _step_context_params(self) -> dict[str, Any]:
        return {"store": False}

    async def _complete(self, messages: list[Message], api_tools: list[Any]) -> Any:
        client = self.runner._get_client()
        t0 = time.perf_counter()
        self.runner._cancel_scope = anyio.CancelScope()
        response = None
        request_id: str | None = None
        try:
            with self.runner._cancel_scope:
                async with client.responses.stream(
                    model=self.runner.model,
                    instructions=self.invocation.system_prompt,
                    input=_imp_to_responses_input(messages),
                    tools=api_tools,
                    store=False,
                    prompt_cache_key=_prompt_cache_key(
                        self.invocation.quest_context.id, self.aid),
                    # §4: enable parallel tool calls so multiple `task` tus can
                    # arrive in one turn. Tools stay strict:True; the weakened
                    # parallel-turn schema guarantee is backstopped by imp's
                    # invoke-time arg validation (is_error on malformed input).
                    parallel_tool_calls=True,
                ) as stream:
                    async for _event in request_bracketed(
                            self.invocation.coordinator, stream):
                        if self.invocation.coordinator is not None:
                            self.invocation.coordinator.note_activity()
                    response = await stream.get_final_response()
                    # No public request-id accessor on the stream and the final
                    # response carries no _request_id; read the `x-request-id`
                    # header off the underlying HTTP response.
                    request_id = stream._response.headers.get("x-request-id")
        finally:
            # N3: reset even on a genuine API error so a later interrupt() never
            # cancels a stale exited scope.
            self.runner._cancel_scope = None
        if response is None:
            return None
        return _serialize_responses_response(response), request_id, t0

    def _parse(self, raw: dict[str, Any]) -> Message:
        return _parse_responses_message(raw)

    def _diagnostics(self, raw: dict[str, Any]) -> Any:
        return _extract_responses_diagnostics(raw)

    def _stop_reason(self, raw: dict[str, Any]) -> str | None:
        return _openai_stop_reason(raw)

    def _emit_web_events(self, raw: dict[str, Any]) -> None:
        _emit_openai_web_events(raw, self.elog, self.aid)

    def _classify_exception(self, exc: BaseException) -> Any:
        return _classify_openai_exception(exc)


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
        session = OpenAISession(invocation, self)
        try:
            try:
                async for ev in run_agent_loop(invocation, session):
                    yield ev
            except perr.ProviderError as exc:
                yield perr.make_error_event(exc)
                return
            except AwaitingUserInput:
                raise  # bare-turn-end / resume-solicit pause unwinds, not classified
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
                    classified = session._classify_exception(exc)
                except Exception:
                    yield perr.make_unknown_error_event(exc)
                    return
                yield perr.make_error_event(classified)
                return
        finally:
            if self._ctx is not None:
                # None guard: self._ctx is None-initialized and assigned
                # mid-open, AFTER the _build_tools/check_web_capability raise
                # sites — the finally runs with None when the generator is
                # aclosed before its first pull or those early raises fire.
                from imp.agents.tools.bg import terminate_background_processes
                with contextlib.suppress(Exception):
                    await terminate_background_processes(self._ctx)
            await self._close_client()

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
