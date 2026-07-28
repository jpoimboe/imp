"""Provider-agnostic push-`Session` base for `run_agent_loop`.

`PushSession` owns the discrete-turn mechanics extracted from the anthropic
runner's old `_run_loop`: transcript load, fresh/resume entry, the per-turn
item stream, the driver push-back surface, and the provider-agnostic Tier-A
proactive-handoff state machine (both push providers inherit it). Provider
specifics (the API call, response parsing, web extras) are the abstract/inert
hooks a subclass overrides — see `AnthropicSession` in `imp.providers.anthropic`.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from typing import Any

from imp.agents.classify import ResumeState, _contains_tool_result, classify_resume
from imp.agents.loop_guard import LoopGuard, TurnRecord
from imp.agents.loop_items import (
    AwaitingToolResults,
    EndOfTurn,
    Error,
    Item,
    Thinking,
    ToolRequested,
    Warning,
)
from imp.agents.runner import Message, TextBlock, ToolResultBlock, ToolUseBlock
from imp.agents.tools import bg as _bg
from imp.agents.tools.context import ToolContext
from imp.agents.transcript import (
    Transcript,
    balance_success_on_resume,
    deliver_continuation_reply,
    deliver_engine_continuation,
    repair_dangling_tool_use_on_resume,
)
from imp.engine.agent_run import (
    _resolve_wait_reply,
    continuation_nudge,
    deliver_interject,
    drain_all_interjects_at_resume,
    last_assistant_text,
    most_recent_assistant_text,
)
from imp.engine.reincarnation import (
    handoff_instruction,
    handoff_path_for,
    write_handoff_note,
)
from imp.providers.catalog import context_threshold_crossed, context_tokens_from_usage
from imp.providers.resolve import spawn_subagent_via_registry


class PushSession:
    def __init__(self, invocation: Any, runner: Any) -> None:
        self.invocation = invocation
        self.runner = runner
        # Attributes below are populated by open().
        self.elog: Any = None
        self.aid: str = ""
        self.ctx: Any = None
        self.tools: dict[str, Any] = {}
        self.guard: LoopGuard | None = None
        self.ceiling: int | None = None
        self.tool_calls_used: int = 0
        self.subagent_depth: int = 0
        self.messages: list[Message] = []
        self.transcript: Any = None
        self._pending_terminal: Item | None = None
        self._handoff_phase: str | None = None
        self._handoff_path: Any = None
        self._crossed_now: bool = False

    # --- lifecycle -------------------------------------------------------
    async def open(self) -> None:
        invocation = self.invocation
        from imp.engine.state import JsonlEventLog
        self.elog = invocation.event_log or JsonlEventLog(invocation.events_path)
        self.aid = invocation.agent_id or "step:unnamed"
        elog = self.elog
        aid = self.aid

        self.transcript = Transcript(invocation.transcript_dir / "transcript.jsonl")
        transcript = self.transcript
        self.tools = self._build_tools()
        ctx = ToolContext(
            project_root=invocation.quest_context.project_root,
            events_path=invocation.events_path,
            subagent_defs=invocation.subagent_defs,
            provider_name=invocation.provider_name,
            model_name=invocation.model_name,
            parent_transcript_dir=invocation.transcript_dir,
            agent_id=aid,
            background_enabled=True,  # full bg surface wired: injection + sweeps + teardown
            quest_home=invocation.quest_context.quest_dir,
        )
        ctx.quest_context = invocation.quest_context  # for subagent dispatch
        ctx.quest_state = invocation.quest_state
        ctx.quest = invocation.quest          # live quest for rename_quest
        ctx.spawn_subagent = spawn_subagent_via_registry
        ctx.resolved_tiers = invocation.resolved_tiers
        ctx.subagent_depth = invocation.subagent_depth
        ctx.coordinator = invocation.coordinator
        ctx.tiers = invocation.tiers
        ctx.test_gates = invocation.test_gates
        ctx.pythonpath = invocation.pythonpath
        ctx.checkpoint_threshold = invocation.checkpoint_threshold
        ctx.non_interactive = invocation.headless
        ctx.base_sandbox_policy = invocation.sandbox_policy
        ctx.event_log = elog                       # shared write instance
        if self.runner._user_input_fn is not None:
            ctx.user_input_fn = self.runner._user_input_fn
        self.ctx = ctx
        self.runner._ctx = ctx

        existing = transcript.load(
            ctx.quest_home / "blobs" if ctx.quest_home is not None else None)
        if existing:
            messages = existing
            self.tool_calls_used = 0  # per-invocation; never inherit history
            # A subagent's done-signal is a bare-turn-end terminal (assistant
            # prose, no complete_step), NOT a complete_step. On resume, re-emit
            # its final text as the deliverable and return — a top-level step in
            # the same shape (AWAITING_USER) solicits the human instead.
            if (invocation.subagent_depth > 0
                    and classify_resume(messages) is ResumeState.AWAITING_USER):
                final_text = last_assistant_text(messages)
                self.messages = messages
                self._pending_terminal = EndOfTurn(last_text=final_text)
                self._finish_open()
                return
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
            # STEP_COMPLETED never arises in-process: this runner returns at
            # complete_step, so a trailing prose turn can never persist over a
            # resolved complete_step here — the new state is inert for the
            # in-process providers (openai/codex mirror this resume block).
            state = classify_resume(messages)
            if state is ResumeState.DANGLING_TOOL_USE:
                self.messages = messages
                self._pending_terminal = Error(payload={
                    "category": "config",
                    "kind": "transcript_invalid",
                    "message": "transcript ended with dangling tool_use",
                })
                self._finish_open()
                return
            if state is ResumeState.AWAITING_USER:
                if ctx.user_input_fn is None:
                    self.messages = messages
                    self._pending_terminal = Error(payload={
                        "category": "config",
                        "kind": "no_user_input_fn",
                        "message": (
                            "AWAITING_USER resume requires user_input_fn; "
                            "none installed by runner"
                        ),
                    })
                    self._finish_open()
                    return
                # _resolve_wait_reply owns the SOLE awaiting_user_input emit and
                # fires it only when it actually solicits (not on a queued
                # interject, not on a retry-continue). Route the reply by source:
                # a human turn ('solicited'/'interject') renders as `>`; a 'nudge'
                # is an internal engine continuation, not a user turn.
                reply, source = await _resolve_wait_reply(
                    invocation.coordinator, ctx.user_input_fn,
                    invocation.resume_should_continue,
                    elog=elog, agent_id=aid,
                    resume_cause=invocation.resume_cause,
                    last_agent_message=last_assistant_text(messages),
                    halt_writer=invocation.halt_writer,
                    halt_clearer=invocation.halt_clearer)
                if source == "nudge":
                    assert isinstance(reply, str)
                    deliver_engine_continuation(
                        messages, transcript, elog, aid, reply)
                else:
                    deliver_continuation_reply(
                        messages, transcript, elog, aid, reply, human=True)
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
                pending_human_tail = (
                    messages[-1].role == "user"
                    and not _contains_tool_result(messages[-1])
                )
                if (invocation.resume_should_continue and drained == 0
                        and not pending_human_tail):
                    deliver_engine_continuation(
                        messages, transcript, elog, aid,
                        continuation_nudge(invocation.resume_cause))
        else:
            messages = []
            for m in invocation.initial_messages:
                messages.append(m)
                transcript.append(m)
            self.tool_calls_used = 0
            # Fresh-entry audit scaffolding. Ordered IMMEDIATELY after the
            # transcript append so the "step_context emitted exactly once per
            # agent_id" invariant holds (resume takes the branch above and
            # emits nothing).
            elog.append_step_context(
                agent_id=aid,
                parent_id_override=invocation.first_event_parent_id,
                provider=invocation.provider_name,
                model=self._step_context_model(),
                tools_schema=self._api_tool_schemas(),
                params=self._step_context_params(),
                max_tool_calls=invocation.max_tool_calls)
            elog.append_engine_message(
                agent_id=aid,
                message=Message(role="system",
                                content=(TextBlock(text=invocation.system_prompt),)),
                subtype="system_prompt")
            for m in invocation.initial_messages:
                if all(isinstance(b, TextBlock) for b in m.content):
                    elog.append_engine_message(
                        agent_id=aid, message=m, subtype="agent_prompt")

        self.messages = messages
        self._finish_open()

    def _finish_open(self) -> None:
        invocation = self.invocation
        # max_tool_calls is opt-in; None means no cap. The engine resolves
        # agent_def -> workflow.defaults -> None and passes the result via
        # invocation.max_tool_calls.
        self.ceiling = invocation.max_tool_calls  # may be None (no cap)
        self.guard = LoopGuard(interactive=invocation.interactive)
        self.subagent_depth = invocation.subagent_depth
        # Tier A proactive-handoff watch state (Mechanism 3). _handoff_phase is
        # None until the threshold crosses, then "instruction_sent" for the one
        # handoff-writing turn that follows.
        self._handoff_phase = None
        self._handoff_path = handoff_path_for(invocation.transcript_dir)
        self._crossed_now = False

    def cancelled(self) -> bool:
        return bool(self.runner._cancel)

    # --- the per-turn item stream ---------------------------------------
    async def events(self) -> AsyncIterator[Item]:
        if self._pending_terminal is not None:
            pending, self._pending_terminal = self._pending_terminal, None
            yield pending
            return

        api_tools = list(self._api_tool_schemas()) + list(self._web_tool_specs())
        completed = await self._complete(self.messages, api_tools)
        if completed is None:            # interrupt() cancelled the request
            return
        raw, request_id, t0 = completed
        assistant_msg = self._parse(raw)
        diag = self._diagnostics(raw)
        for content in diag.thinkings:
            yield Thinking(text=content)
        for w in diag.block_warnings:
            yield Warning(text=w)
        self.messages.append(assistant_msg)
        self.transcript.append(assistant_msg)
        usage = self._record_agent_message(raw, request_id, t0)
        self._on_agent_message(raw, usage)
        self._emit_web_events(raw)
        await _bg._sweep_bg(self.ctx)
        self._note_turn(raw)

        tool_uses = [b for b in assistant_msg.content if isinstance(b, ToolUseBlock)]
        text = "".join(
            b.text for b in assistant_msg.content if isinstance(b, TextBlock))
        if not tool_uses:
            yield EndOfTurn(last_text=text, stop_reason=self._stop_reason(raw))
            return
        for tu in tool_uses:
            yield ToolRequested(id=tu.id, name=tu.name, input=tu.input)
        yield AwaitingToolResults(assistant_text=text)

    # --- driver push-back ------------------------------------------------
    def send_tool_results(self, results: list[ToolResultBlock]) -> None:
        user_msg = self._results_to_message(results)
        self.messages.append(user_msg)
        self.transcript.append(user_msg)

    def send(self, text: Any, *, human: bool) -> None:
        deliver_continuation_reply(
            self.messages, self.transcript, self.elog, self.aid, text, human=human)

    def deliver_turn_boundary_interject(self) -> None:
        deliver_interject(
            self.invocation.coordinator, self.messages, self.transcript,
            self.elog, self.aid)

    def subagent_deliverable(self) -> str:
        # Walk back over empty trailing turns, matching the resume re-emit path.
        return last_assistant_text(self.messages)

    # --- provider-agnostic default hooks --------------------------------
    def _results_to_message(self, results: list[ToolResultBlock]) -> Message:
        return Message(role="user", content=tuple(results))

    def _on_agent_message(self, raw: dict[str, Any], usage: dict[str, Any]) -> None:
        return None

    def _note_turn(self, raw: dict[str, Any]) -> None:
        # Tier A watch: compare this turn's occupancy to the checkpoint
        # threshold (only before a handoff is already in flight).
        self._crossed_now = (
            self._handoff_phase is None
            and self.invocation.checkpoint_threshold is not None
            and context_threshold_crossed(
                raw.get("usage"), self.runner.model,
                self.invocation.checkpoint_threshold))

    def handoff_before_nudge(self, last_text: str) -> str | None:
        if self._handoff_phase == "instruction_sent":
            # The handoff-writing turn ended without a tool: signal the captured
            # handoff and stop (the engine reincarnates).
            write_handoff_note(
                self._handoff_path, most_recent_assistant_text(self.messages))
            return str(self._handoff_path)
        return None

    def handoff_after_tools(self) -> tuple[str, str | None]:
        if self._handoff_phase == "instruction_sent":
            # Provider persists the agent's final reply text to the note, then
            # signals the handoff; the engine reincarnates seeded from it.
            write_handoff_note(
                self._handoff_path, most_recent_assistant_text(self.messages))
            return ("handoff", str(self._handoff_path))
        if self._crossed_now:
            # Approaching the window: inject the agent-authored handoff
            # instruction as the next user turn (the same user-message append
            # interjects use), then capture the note on the following turn.
            instr = handoff_instruction(self._handoff_path)
            hmsg = Message(role="user", content=(TextBlock(text=instr),))
            self.messages.append(hmsg)
            self.transcript.append(hmsg)
            self.elog.append_engine_message(
                agent_id=self.aid, message=hmsg, subtype="continuation")
            self._handoff_phase = "instruction_sent"
            return ("injected", None)
        return ("none", None)

    def captured_complete_step(self) -> dict[str, Any] | None:
        return None

    def posthoc_ceiling_tripped(self) -> bool:
        return False

    def observe_bare_turn(self, last_text: str) -> Any:
        assert self.guard is not None
        return self.guard.observe(TurnRecord(
            assistant_text=last_text, tools=[], has_complete_step=False))

    # --- shared record helper used by events() --------------------------
    def _record_agent_message(self, raw: dict[str, Any], request_id: str | None,
                              t0: float) -> dict[str, Any]:
        usage = raw.get("usage") or {}
        self.elog.append_agent_message(
            agent_id=self.aid, provider=self.invocation.provider_name,
            model=raw.get("model") or self.runner.model,
            raw=raw, usage=usage,
            stop_reason=self._stop_reason(raw),
            context_tokens=context_tokens_from_usage(usage),
            request_id=request_id,
            duration_ms=(time.perf_counter() - t0) * 1000.0)
        return usage

    # --- abstract hooks a subclass MUST implement -----------------------
    def _build_tools(self) -> dict[str, Any]: raise NotImplementedError
    def _api_tool_schemas(self) -> list[Any]: raise NotImplementedError
    def _web_tool_specs(self) -> list[Any]: raise NotImplementedError
    def _step_context_model(self) -> str: raise NotImplementedError
    def _step_context_params(self) -> dict[str, Any]: raise NotImplementedError
    async def _complete(self, messages: list[Message], api_tools: list[Any]) -> Any:
        raise NotImplementedError
    def _parse(self, raw: dict[str, Any]) -> Message: raise NotImplementedError
    def _diagnostics(self, raw: dict[str, Any]) -> Any: raise NotImplementedError
    def _stop_reason(self, raw: dict[str, Any]) -> str | None: raise NotImplementedError
    def _emit_web_events(self, raw: dict[str, Any]) -> None: raise NotImplementedError
    def _classify_exception(self, exc: BaseException) -> Any: raise NotImplementedError
