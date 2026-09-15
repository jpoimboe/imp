# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""Provider-agnostic observe-`Session` base for `run_agent_loop`.

`ObserveSession` owns the observe control skeleton extracted from the
claude runner's old `_run_loop`: the per-turn SDK-message stream, the
tool-host-mediated `complete_step` capture surface, and the LoopGuard
single-feed ordering. Provider specifics (transport, history/resume
translation, isolated-home lifecycle, classify, cancellation) are the abstract
hooks a subclass overrides — see `ClaudeSession` in
`imp.providers.claude`.

The seam mirrors `PushSession` but for the pull-shaped observe providers: the
tool host (an in-process MCP server, claude-p's own tool loop) executes tools
out of band and records a captured `complete_step`, so the driver's end-of-turn
terminal point validates that capture rather than dispatching a tool batch.

Two ordering invariants are load-bearing and match the old loop:
  (1) `events()` feeds the LoopGuard EXACTLY ONCE per no-`complete_step`
      terminal turn (the tools-bearing `_flush_turn_record()`); `observe_bare_turn`
      returns None so the driver never feeds a second time.
  (2) the `_handoff_done` short-circuit yields `EndOfTurn` BEFORE the
      classify/termination check so a completed handoff turn wins over the
      classifier.

`_message_to_items` / `_is_terminal` are async (the claude per-message body
interleaves `await`ed LLM-group flushes, context-usage probes, and cooperative
interrupts); the item/decision ORDERING they enforce is what the goldens pin,
not the sync/async signature.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, cast

from imp.agents.loop_guard import LoopGuard


class ObserveSession:
    def __init__(self, invocation: Any, runner: Any) -> None:
        self.invocation = invocation
        self.runner = runner
        # Driver-contract attributes, populated by open()/_enter().
        self.elog: Any = None
        self.aid: str = ""
        self.ctx: Any = None
        self.tools: dict[str, Any] = {}
        self.guard: LoopGuard | None = None
        self.ceiling: int | None = None
        self.tool_calls_used: int = 0
        self.subagent_depth: int = 0
        self.messages: list[Any] = []
        # A terminal Item stashed by _enter() for the first events() pull (a
        # subagent bare-turn re-emit, a resume error). Mirrors PushSession.
        self._pending_terminal: Any = None
        # Set at the instruction_sent completion; handoff_before_nudge surfaces it.
        self._handoff_done: bool = False
        self._handoff_path: str | None = None
        # One-shot: _is_terminal sets it for a steer/handoff-crossing re-query so
        # _receive loops instead of surfacing the ResultMessage as a driver terminal.
        self._requery_pending: bool = False

    # complete_step capture, read by base methods; the subclass overrides these.
    @property
    def _complete_step_called(self) -> bool:
        return False

    @property
    def _complete_step_args(self) -> dict[str, Any] | None:
        return None

    # --- lifecycle -------------------------------------------------------
    async def open(self) -> None:
        await self._enter()

    def cancelled(self) -> bool:
        return bool(self.runner._cancel)

    # --- the per-turn item stream ---------------------------------------
    async def events(self) -> AsyncIterator[Any]:
        from imp.agents.loop_items import DependencyWaited, EndOfTurn, Error
        from imp.providers import errors as perr
        if self._pending_terminal is not None:
            pending, self._pending_terminal = self._pending_terminal, None
            yield pending
            return
        async for raw in self._receive():
            async for item in self._message_to_items(raw):
                yield item
            for info in self._drain_kills():
                self.elog.append(agent_id=self.aid, type="tool_killed",
                                 payload=info)
            fatal = self._fatal_error()
            if fatal is not None:
                yield Error(payload=fatal)
                return
            if self.ctx.dependency_wait:
                # Before _is_terminal: that is where a captured complete_step
                # becomes the step's output, and a waiting turn has no output.
                yield DependencyWaited(deps=tuple(self.ctx.dependency_wait))
                return
            if await self._is_terminal(raw):
                # LoopGuard single-feed: a no-complete_step terminal turn is fed
                # its tools-bearing record here EXACTLY ONCE (a complete_step turn
                # is fed by the driver's validate_output_or_nudge instead).
                rec = self._flush_turn_record()
                assert self.guard is not None
                if rec is not None and not self._complete_step_called:
                    loop_err = self.guard.observe(rec)
                    if loop_err is not None:
                        yield Error(payload=cast(dict[str, Any], perr.make_error_event(
                            loop_err, kind=getattr(loop_err, "kind", ""))["payload"]))
                        return
                # A completed handoff turn wins over the classifier.
                if self._handoff_done:
                    yield EndOfTurn(last_text=self._last_text())
                    return
                # complete_step / post-hoc ceiling: hand the driver an EndOfTurn;
                # it validates the captured complete_step or trips the ceiling.
                if self._complete_step_called or self.posthoc_ceiling_tripped():
                    # Co-turn tool visibility (old claude.py:2415): a
                    # SUCCESSFUL complete_step still feeds its flushed record's
                    # co-turn tool outcomes to the guard exactly once. Guard state
                    # is discarded on the driver's agent_result return, so this is
                    # observe-only; gate on validity so a to-be-nudged malformed
                    # complete_step is NOT fed here (the driver's validate feeds it).
                    if (rec is not None and self._complete_step_called
                            and self._complete_step_ok()):
                        self.guard.observe(rec)
                    yield EndOfTurn(last_text=self._last_text())
                    return
                err = await self._terminal_error(self._trailing_text())
                if err is not None:
                    yield Error(payload=err)
                    return
                yield EndOfTurn(last_text=self._last_text())
                return

    # --- driver-facing terminal surface ---------------------------------
    def captured_complete_step(self) -> dict[str, Any] | None:
        return self._complete_step_args if self._complete_step_called else None

    def posthoc_ceiling_tripped(self) -> bool:
        return (self.ceiling is not None
                and self.tool_calls_used >= self.ceiling
                and not self._complete_step_called)

    def observe_bare_turn(self, last_text: str) -> Any:
        # The tools-bearing turn record was already fed to the guard inside
        # events(); returning None keeps the single-feed invariant.
        return None

    def send(self, text: Any, *, human: bool) -> None:
        self._reset_capture()
        self._requery(text, human=human)

    def deliver_turn_boundary_interject(self) -> None:
        self._drain_interject()

    def subagent_deliverable(self) -> str:
        from imp.engine.agent_run import last_assistant_text
        return last_assistant_text(self.messages)

    def handoff_before_nudge(self, last_text: str) -> str | None:
        return self._handoff_path if self._handoff_done else None

    def handoff_after_tools(self) -> tuple[str, str | None]:
        # observe never yields AwaitingToolResults, so this is never consulted.
        return ("none", None)

    def _complete_step_ok(self) -> bool:
        # Side-effect-free mirror of validate_output_or_nudge's Ok determination
        # (output_guard.py): a valid complete_step, or one whose folded required
        # fields recover to a valid shape. Never touches the LoopGuard.
        from imp.agents.output_guard import recover_folded_fields
        from imp.engine.errors import LoadError
        from imp.engine.schema import validate_agent_output
        schema = self.invocation.agent_def.output_schema
        args = self._complete_step_args
        assert args is not None
        try:
            validate_agent_output(args, schema)
            return True
        except LoadError:
            if any(f.required and f.name not in args for f in schema.fields):
                repaired = recover_folded_fields(args, schema)
                if repaired != args:
                    try:
                        validate_agent_output(repaired, schema)
                        return True
                    except LoadError:
                        return False
            return False

    # --- abstract hooks a subclass MUST implement -----------------------
    async def _enter(self) -> None: raise NotImplementedError
    def _receive(self) -> AsyncIterator[Any]: raise NotImplementedError
    def _message_to_items(self, raw: Any) -> AsyncIterator[Any]:
        raise NotImplementedError
    def _drain_kills(self) -> list[dict[str, Any]]: raise NotImplementedError
    def _fatal_error(self) -> dict[str, Any] | None: raise NotImplementedError
    async def _is_terminal(self, raw: Any) -> bool: raise NotImplementedError
    def _flush_turn_record(self) -> Any: raise NotImplementedError
    async def _terminal_error(self, last_text: str) -> dict[str, Any] | None:
        raise NotImplementedError
    def _last_text(self) -> str: raise NotImplementedError
    # Error-path text: the trailing message only, never an earlier turn's prose.
    def _trailing_text(self) -> str: raise NotImplementedError
    def _requery(self, text: Any, *, human: bool) -> None: raise NotImplementedError
    def _drain_interject(self) -> None: raise NotImplementedError
    def _reset_capture(self) -> None: raise NotImplementedError
