from __future__ import annotations

import types
from pathlib import Path

from imp.engine.executor import _step_started_payload
from imp.engine.types import Continue, SourceLoc, Step


def _step(*, chrome: str | None) -> Step:
    return Step(
        name="scoping", phase="front-door", kind="agent", rules=(),
        on_max_retries=Continue(), max_loops=1,
        source=SourceLoc(file=Path("workflow.yaml"), line=1),
        chrome=chrome)


def test_payload_carries_chrome_when_set() -> None:
    p = _step_started_payload(_step(chrome="none"),
                              types.SimpleNamespace(route_label=None))
    assert p["chrome"] == "none"


def test_payload_omits_chrome_when_unset() -> None:
    p = _step_started_payload(_step(chrome=None),
                              types.SimpleNamespace(route_label=None))
    assert "chrome" not in p
