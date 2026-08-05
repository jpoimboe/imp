"""docs/guide/embedding.md — the supported embedding surface's contract page."""

from __future__ import annotations

import ast
import re
from dataclasses import fields, is_dataclass
from pathlib import Path

from imp import api
from imp.api import EVENT_SCHEMA_VERSION, Event, QuestHandle, Session

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

_PY_BLOCK = re.compile(r"```python\n(.*?)```", re.DOTALL)

# The names the example binds; every attribute reached through one must be real.
_OWNERS: dict[str, object] = {
    "session": Session, "event": Event, "handle": QuestHandle,
}


def _body() -> str:
    return DOC.read_text()


def _flat() -> str:
    return " ".join(_body().split())


def _example() -> str:
    """The one worked example: the fenced python block that actually runs."""
    blocks = [b for b in _PY_BLOCK.findall(_body()) if "asyncio.run(" in b]
    assert len(blocks) == 1, f"expected exactly one runnable example, found {len(blocks)}"
    return blocks[0]


def _members(owner: object) -> set[str]:
    if is_dataclass(owner):
        return {f.name for f in fields(owner)}
    return {n for n in dir(owner) if not n.startswith("_")}


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


def test_example_is_short_enough_to_read() -> None:
    assert len(_example().strip().splitlines()) <= 50


def test_example_is_valid_python() -> None:
    ast.parse(_example())


def test_example_imports_only_public_names() -> None:
    """A doc example that reaches into `imp.*` internals would teach the one thing
    this API exists to make unnecessary."""
    imported: set[str] = set()
    for node in ast.walk(ast.parse(_example())):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("imp"):
            assert node.module == "imp.api", node.module
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.Import):
            assert not any(a.name.startswith("imp") for a in node.names), "import imp.*"
    assert imported, "the example imports nothing from imp.api"
    assert imported <= set(api.__all__), imported - set(api.__all__)


def test_example_only_touches_real_attributes() -> None:
    """The example is checked against the live classes, so a rename in api.py
    fails here instead of shipping a guide that does not run."""
    checked = 0
    for node in ast.walk(ast.parse(_example())):
        if not (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)):
            continue
        owner = _OWNERS.get(node.value.id)
        if owner is None:
            continue
        assert node.attr in _members(owner), f"{node.value.id}.{node.attr}"
        checked += 1
    assert checked >= 5, f"the example exercises too little of the API ({checked})"
