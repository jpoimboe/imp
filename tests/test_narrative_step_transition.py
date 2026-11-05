from __future__ import annotations

import io
from typing import Any

import pytest

from imp.cli.live_driver import LiveDriver
from imp.cli.live_status import LiveStatus
from imp.cli.narrative import LiteralTextNode, NarrativeBuilder
from imp.cli.render_markdown import render_markdown
from imp.cli.render_text import render_tree

SRC = "alpha-step"
TGT = "beta-step"
OTHER = "gamma-step"
SRC_LABEL = "doing alpha"
TGT_LABEL = "doing beta"
LINE = f"↩ {SRC_LABEL} → {TGT_LABEL}"


def _ev(etype: str, **payload: Any) -> dict[str, Any]:
    return {"id": etype, "agent_id": None, "type": etype, "payload": payload}


def _started(
    step: str, *, label: str | None = None, show_label: bool = True, phase: str = "one",
    phase_label: str | None = None, **extra: Any,
) -> dict[str, Any]:
    payload = {"step": step, "kind": "agent", "phase": phase, "show_label": show_label, **extra}
    if label is not None:
        payload["label"] = label
    if phase_label is not None:
        payload["phase_label"] = phase_label
    return _ev("step_started", **payload)


def _completed(step: str) -> dict[str, Any]:
    return _ev("step_completed", step=step, kind="agent",
               action={"verb": "goto", "target": TGT}, var_mutations={})


def _transition(frm: str = SRC, to: str = TGT, **extra: Any) -> dict[str, Any]:
    return _ev("step_transition", from_step=frm, target_step=to, backward=True, **extra)


def _ran_source(*, label: str | None = SRC_LABEL, show_label: bool = True) -> list[dict[str, Any]]:
    return [_started(SRC, label=label, show_label=show_label), _completed(SRC)]


def _build(events: list[dict[str, Any]]) -> NarrativeBuilder:
    b = NarrativeBuilder()
    for e in events:
        b.add(e)
    return b


def _lines(events: list[dict[str, Any]]) -> list[str]:
    tree = _build(events).tree
    return [ln.strip() for ln in render_tree(tree, full=False, color=False, width=120)]


def _route_lines(events: list[dict[str, Any]]) -> list[str]:
    return [ln for ln in _lines(events) if ln.startswith("↩")]


@pytest.mark.parametrize(
    ("source_visible", "target_visible", "target_label", "expected"),
    [
        (True, True, TGT_LABEL, LINE),
        (True, True, None, f"↩ {SRC_LABEL} → {TGT}"),
        (True, False, TGT_LABEL, LINE),
        (True, False, None, None),
        (False, True, TGT_LABEL, None),
        (False, True, None, None),
        (False, False, TGT_LABEL, f"↩ {TGT_LABEL}"),
        (False, False, None, None),
    ],
)
def test_line_selection(
    source_visible: bool, target_visible: bool, target_label: str | None, expected: str | None,
) -> None:
    events = [
        *_ran_source(show_label=source_visible),
        _transition(),
        _started(TGT, label=target_label, show_label=target_visible),
    ]
    assert _route_lines(events) == ([expected] if expected else [])


def test_hidden_unlabelled_target_adds_no_output() -> None:
    head = [*_ran_source(), _transition()]
    target = [
        _started(TGT, show_label=False, kind="script"),
        _ev("step_completed", step=TGT, kind="script",
            action={"verb": "continue"}, var_mutations={}),
    ]
    assert _lines([*head, *target]) == _lines(head)


def test_source_without_a_label_shows_its_step_name() -> None:
    events = [*_ran_source(label=None), _transition(), _started(TGT, label=TGT_LABEL)]
    assert _route_lines(events) == [f"↩ {SRC} → {TGT_LABEL}"]


def test_source_text_matches_its_completion_line_after_a_label_push() -> None:
    events = [
        _started(SRC, label=SRC_LABEL),
        _ev("step_label_updated", label="pushed words"),
        _completed(SRC),
        _transition(),
        _started(TGT, label=TGT_LABEL),
    ]
    lines = _lines(events)
    assert "pushed words · complete" in lines
    assert f"↩ pushed words → {TGT_LABEL}" in lines


@pytest.mark.parametrize("reason", ["Gate `check` failed (exit 1).", None])
def test_stored_reason_is_ignored(reason: str | None) -> None:
    head = _ran_source()
    target = _started(TGT, label=TGT_LABEL)
    with_reason = _lines([*head, _transition(reason=reason), target])
    without_key = _lines([*head, _transition(), target])
    assert with_reason == without_key
    assert LINE in with_reason


def test_line_sits_between_completion_and_target_header() -> None:
    lines = _lines([*_ran_source(), _transition(), _started(TGT, label=TGT_LABEL)])
    assert (lines.index(f"{SRC_LABEL} · complete")
            < lines.index(LINE)
            < lines.index(TGT_LABEL))


def _phase_crossing_events() -> list[dict[str, Any]]:
    return [
        _started(SRC, label=SRC_LABEL, phase="one", phase_label="first phase"),
        _completed(SRC),
        _transition(),
        _started(TGT, label=TGT_LABEL, phase="two", phase_label="second phase"),
    ]


def test_line_precedes_the_new_phase_header_and_stays_in_the_source_phase() -> None:
    events = _phase_crossing_events()
    lines = _lines(events)
    assert (lines.index(f"first phase · {SRC_LABEL} · complete")
            < lines.index(LINE)
            < lines.index("── second phase ───")
            < lines.index(f"second phase · {TGT_LABEL}"))

    source_phase, target_phase = _build(events).tree.root.children
    routes = [c for c in source_phase.children if isinstance(c, LiteralTextNode)]
    assert [(r.text, r.color_role) for r in routes] == [(LINE, "lifecycle")]
    assert not any(isinstance(c, LiteralTextNode) for c in target_phase.children)


def test_live_view_prints_the_line_before_the_new_phase_header() -> None:
    class _Pipe(io.StringIO):
        def isatty(self) -> bool:
            return False

    out = _Pipe()
    driver = LiveDriver(stream=out, color=False, width=80, status=LiveStatus(out))
    for event in _phase_crossing_events():
        driver.on_event(event)
    driver.finish()
    lines = [ln.strip() for ln in out.getvalue().splitlines()]
    assert lines.count(LINE) == 1
    assert (lines.index(f"first phase · {SRC_LABEL} · complete")
            < lines.index(LINE)
            < lines.index("── second phase ───"))


@pytest.mark.parametrize("resumed_event", [True, False], ids=["resumed", "attached-client"])
def test_pause_holds_the_line_until_the_target_starts(resumed_event: bool) -> None:
    paused = [*_ran_source(), _transition(),
              _ev("workflow_paused", reason="interrupt", interrupt_reason="interrupt")]
    assert _route_lines(paused) == []

    resume = [_ev("workflow_resumed")] if resumed_event else []
    lines = _lines([*paused, *resume,
                    _started(TGT, label=TGT_LABEL, resumed=False, restarted=False)])
    assert lines.count(LINE) == 1
    assert lines.index("paused (interrupt)") < lines.index(LINE) < lines.index(TGT_LABEL)


def test_next_step_other_than_target_discards_the_line() -> None:
    assert _route_lines([*_ran_source(), _transition(),
                         _started(OTHER, label="doing gamma"), _completed(OTHER),
                         _started(TGT, label=TGT_LABEL)]) == []


def test_crash_rerun_of_source_renders_only_the_second_bounce() -> None:
    crashed = [*_ran_source(), _transition(), _ev("workflow_resumed"),
               _started(SRC, label=SRC_LABEL)]
    assert _route_lines(crashed) == []
    assert _route_lines([*crashed, _completed(SRC), _transition(),
                         _started(TGT, label=TGT_LABEL)]) == [LINE]


@pytest.mark.parametrize("paused", [False, True], ids=["retry-branch", "resume-reentry"])
def test_line_renders_when_the_target_node_is_still_open(paused: bool) -> None:
    pause = [_ev("workflow_paused", reason="interrupt", interrupt_reason="interrupt"),
             _ev("workflow_resumed")] if paused else []
    events = [
        _started(TGT, label=TGT_LABEL), *_ran_source(), _transition(), *pause,
        _started(TGT, label=TGT_LABEL, resumed=True, restarted=False, route_label=TGT_LABEL),
    ]
    assert _route_lines(events) == [LINE]


UNMATCHED_SOURCES: list[list[dict[str, Any]]] = [
    [],
    [_started(OTHER, label="doing gamma"), _completed(OTHER)],
    [_started(SRC, label=SRC_LABEL), _completed(SRC), _started(SRC, label=SRC_LABEL)],
]
UNMATCHED_IDS = ["no-prior-step", "other-step-completed", "source-rerun-not-completed"]


@pytest.mark.parametrize("head", UNMATCHED_SOURCES, ids=UNMATCHED_IDS)
def test_unmatched_source_with_visible_target_renders_no_line(
    head: list[dict[str, Any]],
) -> None:
    assert _route_lines([*head, _transition(), _started(TGT, label=TGT_LABEL)]) == []


@pytest.mark.parametrize("head", UNMATCHED_SOURCES, ids=UNMATCHED_IDS)
def test_unmatched_source_with_hidden_labelled_target_names_the_target(
    head: list[dict[str, Any]],
) -> None:
    events = [*head, _transition(), _started(TGT, label=TGT_LABEL, show_label=False)]
    assert _route_lines(events) == [f"↩ {TGT_LABEL}"]


def test_transition_without_a_later_step_never_renders() -> None:
    b = _build([*_ran_source(), _transition()])
    b.finalize()
    lines = render_tree(b.tree, full=False, color=False, width=120)
    assert not any("↩" in ln for ln in lines)


def test_markdown_export_carries_the_same_line() -> None:
    events = [*_ran_source(), _transition(reason="ignored"), _started(TGT, label=TGT_LABEL)]
    assert LINE in _route_lines(events)
    markdown = render_markdown(_build(events).tree, full=False)
    assert LINE in markdown.splitlines()
    assert "ignored" not in markdown
