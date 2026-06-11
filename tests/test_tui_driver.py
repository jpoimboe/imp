from __future__ import annotations

import io

from imp.cli.narrative import build_tree
from imp.cli.render_text import render_tree
from imp.cli.tui_driver import TuiDriver
from tests._event_fixtures import SAMPLE, ev


def _drv(**kw) -> TuiDriver:
    return TuiDriver(color=False, width=80, **kw)


def test_render_full_matches_render_tree_full() -> None:
    drv = _drv()
    for e in SAMPLE:
        drv.on_event(e)
    expected = "\n".join(render_tree(build_tree(SAMPLE), full=True,
                                     color=False, width=80))
    assert drv.render(full=True) == expected


def test_render_lean_matches_render_tree_lean() -> None:
    drv = _drv()
    for e in SAMPLE:
        drv.on_event(e)
    expected = "\n".join(render_tree(build_tree(SAMPLE), full=False,
                                     color=False, width=80))
    assert drv.render(full=False) == expected


def test_print_collapsed_transcript_emits_full_false_with_trailing_newline() -> None:
    drv = _drv()
    for e in SAMPLE:
        drv.on_event(e)
    buf = io.StringIO()
    drv.print_collapsed_transcript(buf)
    expected = "\n".join(render_tree(build_tree(SAMPLE), full=False,
                                     color=False, width=80)) + "\n"
    assert buf.getvalue() == expected


def test_on_event_invokes_invalidate_callback() -> None:
    calls: list[int] = []
    drv = _drv(on_invalidate=lambda: calls.append(1))
    drv.on_event(SAMPLE[0])
    drv.on_event(SAMPLE[1])
    assert calls == [1, 1]


def test_status_text_none_before_any_activity() -> None:
    assert _drv().status_text(0, 1.0) is None


def test_status_text_present_during_activity() -> None:
    drv = TuiDriver(color=True, width=80)
    drv.on_event(ev("1", "step_started", step="s", kind="agent", phase="p"))
    drv.on_event(ev("t1", "tool_call", agent_id="a", id="c1", name="bash",
                    input={"command": "ls"}))
    txt = drv.status_text(0, 1.0)
    assert txt is not None
    assert "bash ls" in txt
