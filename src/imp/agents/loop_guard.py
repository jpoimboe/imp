"""Per-invocation loop detector (spec Fix H).

Replaces the cumulative tool-call ceiling as the default loop signal. Holds
state for ONE agent invocation; fed one TurnRecord per turn by each provider's
loop. Two detectors:

  - repeated_failure: K consecutive identical turn signatures.
  - no_progress:      N consecutive turns making no observable progress.

Progress is defined ONLY in terms of per-turn signals a provider loop can see
(NOT filesystem/commit state, which the loop cannot observe):

  - a complete_step;
  - a write-class tool_use that is new or whose result is_error=False
    (registry names {write_file, edit_file, bash});
  - a read-class tool_use against a NOT-yet-seen target with is_error=False
    (registry names {read_file, grep, glob}). Acquiring new information is
    forward motion, so a read-heavy agent (a code reviewer reading dozens of
    distinct files) does not trip. RE-reading an already-seen target is not
    progress: a stuck agent rotating among the same files still accumulates
    no_progress, and reading the identical target every turn trips
    repeated_failure (constant signature). This read-novelty rule is what keeps
    review/research/plan agents — whose real output is invisible to a write-only
    progress signal — from being falsely paused.

The claude_code adapter MUST canonicalize MCP-prefixed names before building a
TurnRecord (mcp__imp__write_file -> write_file).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from imp.providers.errors import LoopError

WRITE_CLASS_TOOLS = frozenset({"write_file", "edit_file", "bash"})
READ_CLASS_TOOLS = frozenset({"read_file", "grep", "glob"})
DEFAULT_REPEAT_K = 3
DEFAULT_NOPROGRESS_N = 6


@dataclass
class ToolOutcome:
    """One tool call within a turn, paired with the is_error of its result.

    `name` MUST be canonical (no mcp__imp__ prefix). `is_error` is the resulting
    tool_result's error flag; when the result hasn't arrived (rare), pass True
    so it is not mistaken for progress.
    """
    name: str
    tool_use_id: str
    input: dict[str, Any]
    is_error: bool


@dataclass
class TurnRecord:
    """One assistant turn plus its resulting tool outcomes. Built per provider:
    anthropic/openai assemble it within one loop iteration; claude_code
    accumulates across assistant + following-user receive() iterations."""
    assistant_text: str
    tools: list[ToolOutcome] = field(default_factory=list)
    has_complete_step: bool = False
    error_category: str | None = None
    error_kind: str | None = None
    error_message: str | None = None


@dataclass
class LoopGuard:
    repeat_k: int = DEFAULT_REPEAT_K
    noprogress_n: int = DEFAULT_NOPROGRESS_N
    _repeat_count: int = 0
    _noprogress_count: int = 0
    _last_sig: str | None = None
    _seen_write_ids: set[str] = field(default_factory=set)
    _seen_read_keys: set[str] = field(default_factory=set)

    def observe(self, record: TurnRecord) -> LoopError | None:
        """Feed one turn. Returns a LoopError when a detector trips, else None.

        Both detectors hold per-invocation state only (they never consult
        reloaded history), so resume cannot trip them spuriously.
        """
        # Lazy import: importing imp.providers.errors at module scope pulls in
        # the imp.providers package __init__, which eagerly imports the provider
        # modules — and those import this module, forming an import cycle. Import
        # the error type here, at first use, after both modules are loaded.
        from imp.providers.errors import LoopError

        if self._is_progress(record):
            self._repeat_count = 0
            self._noprogress_count = 0
            self._last_sig = None
            return None

        # No progress this turn.
        self._noprogress_count += 1
        sig = self._signature(record)
        if sig == self._last_sig:
            self._repeat_count += 1
        else:
            self._repeat_count = 1
            self._last_sig = sig

        # repeated_failure is the more specific signal — check it first.
        if self._repeat_count >= self.repeat_k:
            exc = LoopError(f"repeated failure x{self._repeat_count}: "
                            f"{self._summary(record)}")
            exc.kind = "repeated_failure"  # type: ignore[attr-defined]
            return exc
        if self._noprogress_count >= self.noprogress_n:
            exc = LoopError(f"no progress for {self._noprogress_count} turns")
            exc.kind = "no_progress"  # type: ignore[attr-defined]
            return exc
        return None

    def _is_progress(self, record: TurnRecord) -> bool:
        if record.has_complete_step:
            return True
        progressed = False
        for t in record.tools:
            if t.name in WRITE_CLASS_TOOLS:
                if not t.is_error:
                    return True
                if t.tool_use_id and t.tool_use_id not in self._seen_write_ids:
                    # A *new* write-class tool_use is progress even before its
                    # result lands; record the id so it isn't recounted.
                    self._seen_write_ids.add(t.tool_use_id)
                    return True
            elif t.name in READ_CLASS_TOOLS and not t.is_error:
                # A successful read of a NOT-yet-seen target acquires new
                # information = progress. Key by the read target (input), not the
                # tool_use_id: re-reading the same file via a fresh id yields no
                # new information and must not count. Record every novel key this
                # turn (don't early-return) so each is "seen" next turn; a stuck
                # agent re-reading seen targets then accumulates no_progress.
                # read_file has offset/limit (tool-ergonomics spec): different
                # windows of the same file must not each count as novel
                # information — a stuck agent could slide the window
                # (offset 1, 2, 3, ...) forever. Key reads by path: drop the
                # range fields before hashing.
                inp = t.input
                if t.name == "read_file":
                    inp = {k: v for k, v in inp.items()
                           if k not in ("offset", "limit")}
                key = f"{t.name}|{json.dumps(inp, sort_keys=True, default=str)}"
                if key not in self._seen_read_keys:
                    self._seen_read_keys.add(key)
                    progressed = True
        return progressed

    def _signature(self, record: TurnRecord) -> str:
        # Priority: provider/API error > failed tool call > stuck text+tools.
        if record.error_category is not None:
            prefix = (record.error_message or "")[:80]
            return f"err|{record.error_category}|{record.error_kind}|{prefix}"
        failed = [t for t in record.tools if t.is_error]
        if failed:
            t = failed[0]
            args = json.dumps(t.input, sort_keys=True, default=str)
            return f"tool|{t.name}|{args}"
        # Benign no-progress turn (no error, no failed tool). Reaching here with
        # a read-class tool means the read targets were all already-seen (a novel
        # read is progress and never reaches _signature). CRITICAL: fold the tool
        # activity into the signature, not just assistant_text. Otherwise turns
        # re-reading DIFFERENT seen files (each with empty assistant_text) all
        # hash to the constant "text|" and falsely trip repeated_failure at K —
        # when rotating among seen files should accumulate no_progress instead.
        # Distinct (seen) reads -> distinct sigs -> repeat counter resets each
        # turn -> no_progress; re-reading the IDENTICAL seen file, or identical
        # stuck prose with no tools -> identical sigs -> repeated_failure.
        tool_sig = ";".join(
            f"{t.name}:{json.dumps(t.input, sort_keys=True, default=str)}"
            for t in record.tools)
        return f"text|{record.assistant_text}|{tool_sig}"

    def _summary(self, record: TurnRecord) -> str:
        if record.error_category is not None:
            return f"{record.error_category}/{record.error_kind}"
        for t in record.tools:
            if t.is_error:
                return f"failed {t.name}"
        return (record.assistant_text or "")[:80]
