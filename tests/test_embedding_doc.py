"""docs/guide/embedding.md — the supported embedding surface's contract page."""

from __future__ import annotations

from pathlib import Path

from imp import api
from imp.api import EVENT_SCHEMA_VERSION

DOC = Path("docs/guide/embedding.md")

# Scope 7: the event types the contract documents as relied-upon.
RELIED_ON = (
    "step_started", "step_completed", "agent_message", "tool_call", "tool_result",
    "agent_result", "awaiting_user_input", "user_input_resolved", "workflow_paused",
    "workflow_completed", "replay_complete",
)

# Scope 6: every value `QuestSummary.status` can take, in-flight then finished.
STATUSES = (
    "new", "running", "waiting", "paused", "error", "merge_blocked",
    "merged", "done", "abandoned", "shelved",
)


def _body() -> str:
    return DOC.read_text()


def _flat() -> str:
    return " ".join(_body().split())


def test_embedding_doc_exists_and_is_nonempty() -> None:
    assert DOC.is_file()
    assert _body().strip()


def test_embedding_doc_names_every_exported_symbol() -> None:
    """The page is the contract for the whole export surface: growing `imp.api`
    without documenting the new name fails here."""
    body = _body()
    missing = [name for name in api.__all__ if name not in body]
    assert missing == [], f"undocumented public names: {missing}"


def test_embedding_doc_quotes_the_live_schema_version() -> None:
    assert f"`EVENT_SCHEMA_VERSION` is `{EVENT_SCHEMA_VERSION}`" in _flat()


def test_embedding_doc_lists_every_relied_on_event_type() -> None:
    body = _body()
    for etype in RELIED_ON:
        assert etype in body, etype


def test_embedding_doc_states_the_unknown_type_rule() -> None:
    assert ("An event type you do not recognize must be ignored, never treated as "
            "an error.") in _flat()


def test_embedding_doc_documents_the_replay_handshake() -> None:
    body = _body()
    flat = _flat()
    assert '"_replay": true' in body
    assert "replay_complete" in body
    # the load-bearing warning: the same gate appears in replay AND live
    assert "Key on the live copy of `awaiting_user_input`" in flat
    assert "Keying on the first sighting of the gate is a latent bug" in flat


def test_embedding_doc_documents_the_lease_and_its_consequence() -> None:
    flat = _flat()
    assert "`lease=True` is the default" in flat
    assert "the run pauses and the daemon exits" in flat
    assert "lease=False" in flat


def test_embedding_doc_names_every_summary_status() -> None:
    """Scanning the whole page would pass on prose: "new", "error", "running" and
    "done" all occur elsewhere. Pin the status sentence itself, so each name has to
    be there as a documented status rather than as an incidental English word."""
    flat = _flat()
    start = flat.index("`status` is ")
    sentence = flat[start:flat.index(".", start) + 1]
    for status in STATUSES:
        assert f"`{status}`" in sentence, status


def test_embedding_doc_documents_creation_preflight() -> None:
    flat = _flat()
    assert "no quest home created" in flat
    assert "default-base" in flat


def test_embedding_doc_documents_the_concurrency_model() -> None:
    flat = _flat()
    assert "One quest, one daemon process." in flat
    # the os.chdir constraint is documented as untouched, not worked around
    assert "os.chdir" in flat


def test_embedding_doc_documents_the_security_boundary() -> None:
    flat = _flat()
    assert "0700" in flat
    assert "imp binds no network port" in flat
