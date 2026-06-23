# tests/test_claude_code_session_store.py
from __future__ import annotations

from pathlib import Path

import anyio

from imp.providers.claude_code_session_store import ImpSessionStore


def _main_key(session_id: str = "s1", project_key: str = "/home/u/proj") -> dict:
    return {"project_key": project_key, "session_id": session_id}


def test_append_then_load_round_trips_entries_deep_equal(tmp_path: Path) -> None:
    store = ImpSessionStore(tmp_path)
    key = _main_key()
    batch1 = [{"type": "user", "uuid": "a", "message": {"role": "user"}}]
    batch2 = [{"type": "assistant", "uuid": "b", "message": {"role": "assistant"}}]
    anyio.run(store.append, key, batch1)
    anyio.run(store.append, key, batch2)

    loaded = anyio.run(store.load, key)
    assert loaded == batch1 + batch2


def test_load_unknown_key_returns_none(tmp_path: Path) -> None:
    store = ImpSessionStore(tmp_path)
    assert anyio.run(store.load, _main_key("never-written")) is None


def test_project_key_with_slashes_stays_under_root(tmp_path: Path) -> None:
    store = ImpSessionStore(tmp_path)
    anyio.run(store.append, _main_key(project_key="/etc/../escape"),
              [{"type": "user", "uuid": "a"}])
    written = list(tmp_path.rglob("*.jsonl"))
    assert written, "expected a JSONL file to be written"
    for p in written:
        assert tmp_path in p.parents


def test_dot_only_project_key_cannot_escape_root(tmp_path: Path) -> None:
    store = ImpSessionStore(tmp_path)
    anyio.run(store.append, _main_key(project_key=".."),
              [{"type": "user", "uuid": "a"}])
    written = list(tmp_path.rglob("*.jsonl"))
    assert written, "expected a JSONL file to be written"
    for p in written:
        assert tmp_path in p.parents
    assert anyio.run(
        store.load, _main_key(project_key="..")) == [{"type": "user", "uuid": "a"}]


def test_append_empty_is_a_true_noop(tmp_path: Path) -> None:
    store = ImpSessionStore(tmp_path)
    key = _main_key()
    anyio.run(store.append, key, [])
    # No file created → load still signals "no session" rather than empty.
    assert anyio.run(store.load, key) is None
    assert list(tmp_path.rglob("*.jsonl")) == []


def test_subpath_key_routes_to_separate_file(tmp_path: Path) -> None:
    store = ImpSessionStore(tmp_path)
    main = _main_key()
    sub = {**main, "subpath": "subagents/agent-1"}
    anyio.run(store.append, main, [{"type": "user", "uuid": "m"}])
    anyio.run(store.append, sub, [{"type": "user", "uuid": "a"}])

    assert anyio.run(store.load, main) == [{"type": "user", "uuid": "m"}]
    assert anyio.run(store.load, sub) == [{"type": "user", "uuid": "a"}]
