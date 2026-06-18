"""Generic live-view no-stall invariants over a corpus of REAL journaled runs.

Why this exists (2026-06-12 regression): the live view froze at `spec-writer` —
no step rendered beyond the opener — while the engine ran to completion on disk.
Every existing live test missed it because they assert the body AFTER
`LiveDriver.finish()`, whose `_walk(force=True)` force-flushes any node stuck
blocking the emit cursor. So a permanent MID-RUN stall is invisible to a
finish()-terminated replay (and to `imp log`, and to the live/log parity golden).

These invariants replay each fixture WITHOUT finish() — the real-time, mid-run
state — and assert two things that hold for ANY blocking-node regression (a held
narration, an un-resolving forward-goto breadcrumb, a never-settling subagent
fold, a quiet step whose routing never fills), not just the specific bug:

1. No already-started step is withheld: once step S+1 starts, every earlier
   non-quiet step must already be rendered. The only content the live walk may
   legitimately hold back is the CURRENTLY-open step's in-flight tail.
2. Held narrations never accumulate: at every incremental point at most ONE
   `AgentTextNode.held` exists per container (only the trailing narration can
   still fold into the step conclusion). This catches the root cause at the
   source, before it ever reaches the renderer.

The corpus is real opus/sonnet runs (large string bodies truncated for size —
the invariants are structural, not byte-goldens). Real LLM event shape (prose
interleaved with thinking/tools, wait_for_user, rename_change) is exactly what
the curated single-fixture goldens did not exercise.
"""
from __future__ import annotations

import gzip
import io
import json
from pathlib import Path

import pytest

from imp.cli import narrative as nv
from imp.cli.live_driver import LiveDriver
from imp.cli.live_status import LiveStatus
from tests.conftest import FIXTURES

_CORPUS = sorted((FIXTURES / "live_corpus").glob("*.jsonl.gz"))


class _Pipe(io.StringIO):
    def isatty(self) -> bool:
        return False


def _events(path: Path) -> list[dict]:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _live_no_finish(events: list[dict]) -> str:
    """Replay through the real LiveDriver but DO NOT call finish() — this is the
    mid-run state a watching human sees, with no force-flush to paper over a
    stalled cursor."""
    out = _Pipe()
    drv = LiveDriver(stream=out, color=False, width=80, status=LiveStatus(out))
    for e in events:
        drv.on_event(e)
    return out.getvalue()


def _containers(root: object) -> list[object]:
    """Every node in the tree that holds children (root / phases / steps)."""
    acc: list[object] = []
    stack = [root]
    while stack:
        n = stack.pop()
        kids = getattr(n, "children", None)
        if isinstance(kids, list):
            acc.append(n)
            stack.extend(kids)
    return acc


def test_corpus_is_present() -> None:
    # Guard against the glob silently going empty (renamed/removed fixtures) and
    # turning every parametrized invariant below into a vacuous pass.
    assert _CORPUS, "no live_corpus fixtures found — invariants would not run"


@pytest.mark.parametrize("path", _CORPUS, ids=lambda p: p.stem)
def test_no_completed_step_is_withheld_from_live_view(path: Path) -> None:
    events = _events(path)
    starts = [
        (i, e["payload"]["step"]) for i, e in enumerate(events)
        if e.get("type") == "step_started"
    ]
    assert len(starts) >= 2, f"{path.name}: need >1 step to test withholding"
    last_start_i = starts[-1][0]
    # A non-quiet step renders a `{name} complete` conclusion breadcrumb (agent,
    # command, and chrome steps all do; quiet steps collapse to a routing arrow or
    # nothing, so they have no reliable rendered marker and are excluded). Any step
    # whose step_completed fired BEFORE the currently-open step started is finished
    # and its conclusion must be on screen — withholding it means a stalled cursor.
    non_quiet = {
        e["payload"]["step"] for e in events
        if e.get("type") == "step_started" and not e["payload"].get("quiet")
    }
    completed_before_open = {
        e["payload"].get("step") for i, e in enumerate(events)
        if e.get("type") == "step_completed" and i < last_start_i
    }
    expect = {n for n in completed_before_open if n in non_quiet}

    body = _live_no_finish(events)
    withheld = sorted(n for n in expect if f"{n} complete" not in body)
    assert not withheld, (
        f"{path.name}: live view (no finish) withheld completed steps "
        f"{withheld} — the emit cursor is stalled. Rendered body:\n{body[:600]}"
    )


@pytest.mark.parametrize("path", _CORPUS, ids=lambda p: p.stem)
def test_held_narration_never_accumulates(path: Path) -> None:
    events = _events(path)
    out = _Pipe()
    drv = LiveDriver(stream=out, color=False, width=80, status=LiveStatus(out))
    for e in events:
        drv.on_event(e)
        for cont in _containers(drv._builder.tree.root):
            held = [
                c for c in cont.children  # type: ignore[attr-defined]
                if isinstance(c, nv.AgentTextNode) and c.held
            ]
            assert len(held) <= 1, (
                f"{path.name}: {len(held)} held narrations accumulated in "
                f"{getattr(cont, 'name', type(cont).__name__)!r} after a "
                f"{e.get('type')} event — only the trailing narration may be held. "
                f"Stale held nodes permanently block the live emit cursor."
            )


@pytest.mark.parametrize("path", _CORPUS, ids=lambda p: p.stem)
def test_dropped_first_step_started_still_renders_later_headers(path: Path) -> None:
    # Simulate the attach race that replay closes: the first step_started is lost.
    # The dropped step legitimately loses its own headers, but EVERY later step
    # completed before the final open step must still render its conclusion — a
    # stalled cursor (the catastrophic bug) withholds them all.
    events = _events(path)
    first_i = next(i for i, e in enumerate(events) if e.get("type") == "step_started")
    dropped = events[first_i]["payload"]["step"]
    skip = events[:first_i] + events[first_i + 1:]

    starts = [i for i, e in enumerate(skip) if e.get("type") == "step_started"]
    assert starts, f"{path.name}: no step_started survives the drop"
    last_start_i = starts[-1]
    non_quiet = {
        e["payload"]["step"] for e in skip
        if e.get("type") == "step_started" and not e["payload"].get("quiet")
    }
    completed_before_open = {
        e["payload"].get("step") for i, e in enumerate(skip)
        if e.get("type") == "step_completed" and i < last_start_i
    }
    expect = {n for n in completed_before_open if n in non_quiet and n != dropped}
    assert expect, f"{path.name}: need a later completed step to test"

    body = _live_no_finish(skip)
    withheld = sorted(n for n in expect if f"{n} complete" not in body)
    assert not withheld, (
        f"{path.name}: after dropping the first step_started, the live view "
        f"withheld later completed steps {withheld} — the cursor stalled."
    )
