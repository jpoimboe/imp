from __future__ import annotations

import re
from pathlib import Path

from imp.cli import narrative as nv
from imp.cli.store import _resolve_intent

_EVENT_ID_NS_RE = re.compile(r"""id_ns["']?\s*(?:==|=|:)\s*["']event["']""")

_OLD_TYPES = (
    "opening_intent", "llm_response", "agent_output",
    "engine_continuation", "completion_nudge", "user_interjected",
)


def test_narrative_builder_has_no_handler_for_old_event_types() -> None:
    handlers = nv.NarrativeBuilder._HANDLERS
    for old in _OLD_TYPES:
        assert old not in handlers, f"reader special-cases old type {old!r}"
    for new in ("user_message", "engine_message", "agent_message"):
        assert new in handlers


def test_title_resolver_ignores_old_opening_intent_shape() -> None:
    old = [{"id": "i0", "parent_id": None, "agent_id": None, "timestamp": "T0",
            "type": "opening_intent", "payload": {"text": "real opening"}}]
    assert _resolve_intent(old) is None


def test_no_source_probes_or_writes_the_deleted_event_id_ns() -> None:
    # Single-version reader: `"event"` has no writers and no probe. The guard is a
    # regex over the code spellings in either quote style (ruff selects no quote rules,
    # so single quotes are legal here); prose that names the old value without an
    # `=`/`:` is not a false positive.
    imp_src = Path(nv.__file__).parent.parent
    offenders = sorted(
        str(p.relative_to(imp_src)) for p in imp_src.rglob("*.py")
        if _EVENT_ID_NS_RE.search(p.read_text(encoding="utf-8")))
    assert offenders == []
