from __future__ import annotations

from pathlib import Path

from imp.cli import narrative as nv
from imp.cli.store import _resolve_intent
from imp.engine.event_migrations import migrate_events

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


def test_title_resolver_reads_migrated_intent() -> None:
    old = [{"id": "i0", "parent_id": None, "agent_id": None, "timestamp": "T0",
            "type": "opening_intent", "payload": {"text": "real opening"}}]
    assert _resolve_intent(migrate_events(old)) == "real opening"


def test_generic_human_turn_fallback_keys_on_new_format_reply() -> None:
    # No intent event: the resolver falls back to the first genuine human turn.
    # It requires no old-format shape or `human` key — a regression that keyed on
    # `human: True` would return None on the migrated (human-stripped) payload.
    pre_subtype_user_message = [{"id": "u1", "parent_id": None, "agent_id": "A", "timestamp": "T0",
            "type": "user_message",
            "payload": {"role": "user",
                        "content": [{"type": "text", "text": "typed reply"}],
                        "human": True}}]
    assert _resolve_intent(migrate_events(pre_subtype_user_message)) == "typed reply"


def test_no_source_probes_or_writes_the_deleted_event_id_ns() -> None:
    # Single-version reader: `"event"` has no writers and no probe. The tokens are the
    # three CODE spellings; migration prose names the old value as "event" id_ns (no
    # `=`), so a docstring that mentions it is not a false positive.
    imp_src = Path(nv.__file__).parent.parent
    tokens = ('id_ns == "event"', 'id_ns="event"', '"id_ns": "event"')
    offenders = sorted(
        str(p.relative_to(imp_src)) for p in imp_src.rglob("*.py")
        if any(t in p.read_text(encoding="utf-8") for t in tokens))
    assert offenders == []
