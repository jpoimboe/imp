from __future__ import annotations

import types
from pathlib import Path

from imp.agents.random_status import RANDOM_STATUS_PHRASES, STATUS_PHRASE_LABEL
from imp.engine.executor import _step_started_payload
from imp.engine.types import Continue, SourceLoc, Step


def _step(*, label: str | None) -> Step:
    return Step(
        name="front-door", phase="front-door", kind="agent", rules=(),
        on_max_retries=Continue(), max_loops=1,
        source=SourceLoc(file=Path("workflow.yaml"), line=1),
        label=label)


def test_sentinel_label_sets_rotate_phrase_and_a_concrete_frozen_phrase() -> None:
    p = _step_started_payload(_step(label=STATUS_PHRASE_LABEL),
                              types.SimpleNamespace(route_label=None))
    assert p["rotate_phrase"] is True
    assert p["label"] in RANDOM_STATUS_PHRASES      # frozen phrase, not "<random>"


def test_plain_label_carries_no_rotate_phrase_flag() -> None:
    p = _step_started_payload(_step(label="implementing"),
                              types.SimpleNamespace(route_label=None))
    assert "rotate_phrase" not in p
    assert p["label"] == "implementing"
