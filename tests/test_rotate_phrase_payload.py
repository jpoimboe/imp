from __future__ import annotations

import types
from pathlib import Path

from imp.engine.executor import _step_started_payload
from imp.engine.types import Continue, SourceLoc, Step


def _step(*, label: str | None = None, interactive: bool = False,
          rotate_phrase: bool | None = None) -> Step:
    return Step(
        name="s", phase="p", kind="agent", rules=(),
        on_max_retries=Continue(), max_loops=1,
        source=SourceLoc(file=Path("workflow.yaml"), line=1),
        label=label, interactive=interactive, rotate_phrase=rotate_phrase)


def _payload(step: Step) -> dict:
    return _step_started_payload(step, types.SimpleNamespace(route_label=None))


def test_explicit_rotate_phrase_true_sets_flag_and_keeps_the_literal_label() -> None:
    p = _payload(_step(label="task 1/3", rotate_phrase=True))
    assert p["rotate_phrase"] is True
    assert p["label"] == "task 1/3"                 # the label is the literal, never a phrase


def test_interactive_unset_rotate_phrase_defaults_on() -> None:
    p = _payload(_step(label="scoping the change", interactive=True))
    assert p["rotate_phrase"] is True


def test_non_interactive_unset_rotate_phrase_stays_off() -> None:
    p = _payload(_step(label="writing plan"))
    assert "rotate_phrase" not in p
    assert p["label"] == "writing plan"


def test_interactive_rotate_phrase_false_forces_the_label() -> None:
    p = _payload(_step(label="reviewing spec", interactive=True, rotate_phrase=False))
    assert "rotate_phrase" not in p
    assert p["label"] == "reviewing spec"
