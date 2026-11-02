# tests/test_resume_truncation_property.py
"""Truncation property test: at every cut of a real-shaped, one-Message-per-block
claude transcript, the interrupt repairs leave a paired, marker-free tail that
carries imp's captured results and at most one durability note; at every
complete_step tail shape, the complete_step answer loads the framed feedback or a
success behind the call and trails one redo nudge."""
from __future__ import annotations

import json
from collections import Counter
from itertools import pairwise
from pathlib import Path

import pytest

from imp.agents.runner import (
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    canonicalize_tool_name,
    message_from_json,
)
from imp.agents.transcript import (
    RESUME_DURABILITY_NOTE,
    RESUME_REDO_NUDGE,
    REWORK_RESULT_FRAME,
    Transcript,
    _is_interrupt_marker,
    answer_complete_step_on_resume,
    answer_unanswered_tool_uses_on_resume,
    captured_result_key,
    complete_step_tail,
    repair_interrupt_marker_on_resume,
    unanswered_tool_uses,
)
from imp.engine.state import InMemoryEventLog
from imp.providers.claude import _project_for_cli, _stream_start

_FIXTURE = Path(__file__).parent / "fixtures" / "claude_per_block_transcript.jsonl"
_REJECTION = (
    "The user doesn't want to proceed with this tool use. The tool use was rejected "
    "(eg. if it was a file edit, the new_string was NOT written to the file). STOP what "
    "you are doing and wait for the user to tell you how to proceed.")
_MARK_PLAIN = "[Request interrupted by user]"
_MARK_TOOL = "[Request interrupted by user for tool use]"


def _load() -> list[Message]:
    return [message_from_json(json.loads(line))
            for line in _FIXTURE.read_text().splitlines() if line.strip()]


_MSGS = _load()


def _results(m: Message) -> list[ToolResultBlock]:
    return [b for b in m.content if isinstance(b, ToolResultBlock)]


def _tool_uses(m: Message) -> list[ToolUseBlock]:
    return [b for b in m.content if isinstance(b, ToolUseBlock)]


def _is_cs(tu: ToolUseBlock) -> bool:
    return canonicalize_tool_name(tu.name) == "complete_step"


def _u(*blocks: TextBlock | ToolResultBlock) -> Message:
    return Message(role="user", content=blocks)


def _rejected(tu: ToolUseBlock) -> Message:
    return _u(ToolResultBlock(tool_use_id=tu.id, content=_REJECTION, is_error=True))


def _trailing_user_start(msgs: list[Message]) -> int:
    i = len(msgs)
    while i > 0 and msgs[i - 1].role == "user":
        i -= 1
    return i


def _variants(pre: list[Message]) -> list[tuple[str, list[Message], ToolResultBlock | None]]:
    out: list[tuple[str, list[Message], ToolResultBlock | None]] = [
        ("bare", pre, None),
        ("marker", [*pre, _u(TextBlock(text=_MARK_PLAIN))], None),
        ("marker+text", [*pre, _u(TextBlock(text=_MARK_PLAIN)),
                         _u(TextBlock(text="please continue"))], None),
    ]
    outstanding = unanswered_tool_uses(pre)
    if outstanding:
        out.append(("cli", [*pre, *map(_rejected, outstanding),
                            _u(TextBlock(text=_MARK_TOOL))], None))
        real = ToolResultBlock(tool_use_id=outstanding[0].id,
                               content=f"REAL-{outstanding[0].id}", is_error=False)
        out.append(("cli+real", [*pre, _u(real), *map(_rejected, outstanding[1:]),
                                 _u(TextBlock(text=_MARK_TOOL))], real))
    return out


def _captured(outstanding: list[ToolUseBlock], real: ToolResultBlock | None,
              mode: str) -> dict[str, tuple[str, bool]]:
    """`half`: imp captured a success for every even-indexed outstanding call (with
    the content that really ran, for the one that got a real result). complete_step
    is never captured: its result is the complete_step-tail answer's to set."""
    if mode == "none":
        return {}
    cap: dict[str, tuple[str, bool]] = {}
    for i, tu in enumerate(t for t in outstanding if not _is_cs(t)):
        if i % 2 == 0:
            content = (str(real.content) if real is not None and real.tool_use_id == tu.id
                       else f"CAPTURED-{tu.id}")
            cap[captured_result_key(tu.name, tu.input)] = (content, False)
    return cap


def _pairing_error(proj: list[Message]) -> str | None:
    issued: set[str] = set()
    for i, m in enumerate(proj):
        if i and proj[i - 1].role == m.role:
            return f"consecutive {m.role} at {i}"
        ids = {b.id for b in _tool_uses(m)}
        if m.role == "assistant" and ids:
            nxt = proj[i + 1] if i + 1 < len(proj) else None
            answered = {b.tool_use_id for b in _results(nxt)} if nxt is not None else set()
            if not ids <= answered:
                return f"unanswered {sorted(ids - answered)} at {i}"
        issued |= ids
        for b in _results(m):
            if b.tool_use_id not in issued:
                return f"orphan {b.tool_use_id} at {i}"
    return None


def _check(inp: list[Message], out: list[Message],
           captured: dict[str, tuple[str, bool]]) -> list[str]:
    fails: list[str] = []
    proj = _project_for_cli(out)
    if (e := _pairing_error(proj)) is not None:
        fails.append(f"i:{e}")
    counts = Counter(b.tool_use_id for m in proj for b in _results(m))
    if dupes := [i for i, n in counts.items() if n > 1]:
        fails.append(f"i:duplicate results {sorted(dupes)}")
    by_id = {b.tool_use_id: b for m in proj for b in _results(m)}
    cs_ids = {t.id for m in inp for t in _tool_uses(m) if _is_cs(t)}
    if any(_REJECTION in str(b.content) for i, b in by_id.items() if i not in cs_ids):
        fails.append("ii:rejection copy survives")
    for tu in (t for m in inp for t in _tool_uses(m)):
        hit = captured.get(captured_result_key(tu.name, tu.input))
        got = by_id.get(tu.id)
        if hit is not None and (got is None or got.content != hit[0]):
            fails.append(f"iii:{tu.id} lost its captured result")
    tail = out[_trailing_user_start(out):]
    notes = sum(1 for m in tail for b in m.content
                if isinstance(b, TextBlock) and b.text == RESUME_DURABILITY_NOTE)
    if notes > 1:
        fails.append(f"iv:{notes} notes")
    inp_tail = inp[_trailing_user_start(inp):]
    by_design = (not any(_results(m) for m in inp_tail)
                 and not unanswered_tool_uses(inp))
    if any(_is_interrupt_marker(m) for m in tail) and not by_design:
        fails.append("v:marker remains")
    return fails


def test_fixture_is_real_shaped() -> None:
    """The matrix only means something over the shapes the census found."""
    parallel = any(
        _tool_uses(a) and _tool_uses(b) and a.role == b.role == "assistant"
        for a, b in pairwise(_MSGS))
    pending: dict[str, int] = {}
    interleaved = False
    for i, m in enumerate(_MSGS):
        for r in _results(m):
            pending.pop(r.tool_use_id, None)
        for tu in _tool_uses(m):
            if pending and any(_results(x) for x in _MSGS[min(pending.values()):i]):
                interleaved = True
            pending[tu.id] = i
    a, r, k = _MSGS[-3:]
    stored_cs = (any(_is_cs(t) for t in _tool_uses(a))
                 and [(b.tool_use_id, b.is_error) for b in _results(r)]
                 == [(_tool_uses(a)[0].id, False)]
                 and _is_interrupt_marker(k))
    assert parallel and interleaved and stored_cs


@pytest.mark.parametrize("mode", ["none", "half"])
def test_interrupt_repairs_hold_at_every_truncation(tmp_path: Path, mode: str) -> None:
    transcript = Transcript(tmp_path / "transcript.jsonl")
    failures: list[tuple[int, str, list[str]]] = []
    for k in range(2, len(_MSGS) + 1):
        pre = _MSGS[:k]
        outstanding = unanswered_tool_uses(pre)
        for name, inp, real in _variants(pre):
            captured = _captured(outstanding, real, mode)
            out = list(inp)
            repair_interrupt_marker_on_resume(out, transcript, captured=captured)
            answer_unanswered_tool_uses_on_resume(
                out, transcript, InMemoryEventLog(), "step:p/s", captured=captured)
            if fails := _check(inp, out, captured):
                failures.append((k, name, fails))
    assert failures == []


def _cs_cases() -> list[tuple[str, ToolUseBlock, list[Message]]]:
    out: list[tuple[str, ToolUseBlock, list[Message]]] = []
    for ci, m in enumerate(_MSGS):
        for cs in filter(_is_cs, _tool_uses(m)):
            dangling = _MSGS[:ci + 1]
            ri = next(i for i in range(ci + 1, len(_MSGS))
                      if any(r.tool_use_id == cs.id for r in _results(_MSGS[i])))
            stored = _MSGS[:ri + 1]
            marked = [*stored, _u(TextBlock(text=_MARK_PLAIN))]
            out += [
                (f"{cs.id}:dangling", cs, dangling),
                (f"{cs.id}:dangling+marker", cs,
                 [*dangling, _u(TextBlock(text=_MARK_PLAIN))]),
                (f"{cs.id}:race", cs, [*dangling, _rejected(cs),
                                       _u(TextBlock(text=_MARK_TOOL))]),
                (f"{cs.id}:stored", cs, stored),
                (f"{cs.id}:stored+marker", cs, marked),
                (f"{cs.id}:stored+marker+text", cs,
                 [*marked, _u(TextBlock(text="OLD FEEDBACK"))]),
            ]
    return out


def _resume_cs(msgs: list[Message], transcript: Transcript, elog: InMemoryEventLog,
               feedback: str | None) -> ToolUseBlock | None:
    repair_interrupt_marker_on_resume(msgs, transcript)
    cs = complete_step_tail(msgs)
    if cs is not None:
        answer_complete_step_on_resume(msgs, transcript, elog, "step:p/s", cs, feedback)
    answer_unanswered_tool_uses_on_resume(msgs, transcript, elog, "step:p/s")
    return cs


def _check_cs(cs: ToolUseBlock, out: list[Message], elog: InMemoryEventLog,
              feedback: str | None) -> list[str]:
    fails: list[str] = []
    if (err := _pairing_error(_project_for_cli(out))) is not None:
        fails.append(f"i:{err}")
    tail = out[_trailing_user_start(out):]
    if any(_is_interrupt_marker(m) for m in tail):
        fails.append("v:marker remains")
    got = [b for m in out for b in _results(m) if b.tool_use_id == cs.id]
    if feedback:
        ok = [(b.content, b.is_error) for b in got] == [
            (REWORK_RESULT_FRAME + "\n\n" + feedback, True)]
    else:
        ok = len(got) == 1 and not got[0].is_error
    if not ok:
        fails.append(f"e:result {[(b.content, b.is_error) for b in got]}")
    texts = [b.text for m in tail for b in m.content if isinstance(b, TextBlock)]
    if RESUME_DURABILITY_NOTE in texts:
        fails.append("e:durability note")
    if out[-1] != _u(TextBlock(text=RESUME_REDO_NUDGE)) or texts.count(RESUME_REDO_NUDGE) != 1:
        fails.append("e:redo nudge")
    if feedback and any(isinstance(b, TextBlock) and feedback in b.text
                        for m in out for b in m.content):
        fails.append("e:feedback as text")
    if any(e["type"] == "tool_result" and e["payload"]["id"] == cs.id
           for e in elog.read()):
        fails.append("e:tool_result event")
    if _project_for_cli(out[:_stream_start(out)])[-1].role != "user":
        fails.append("e:body ends on assistant")
    return fails


@pytest.mark.parametrize("feedback", [None, "REVISE: fix X"])
def test_complete_step_tail_answers_hold_at_every_shape(
        tmp_path: Path, feedback: str | None) -> None:
    transcript = Transcript(tmp_path / "transcript.jsonl")
    cases = _cs_cases()
    failures: list[tuple[str, list[str]]] = []
    for name, cs, inp in cases:
        out, elog = list(inp), InMemoryEventLog()
        if _resume_cs(out, transcript, elog, feedback) != cs:
            failures.append((name, ["tail: complete_step not found"]))
            continue
        fails = _check_cs(cs, out, elog, feedback)
        for refeed in (None, feedback):
            again = list(out)
            _resume_cs(again, transcript, InMemoryEventLog(), refeed)
            if again != out:
                fails.append(f"re-resume ({refeed!r}) changed the transcript")
        if fails:
            failures.append((name, fails))
    assert cases and failures == []
