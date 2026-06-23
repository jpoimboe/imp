# tests/test_claude_code_session_store.py
from __future__ import annotations

import tempfile
import uuid
from pathlib import Path

import anyio
from claude_agent_sdk.testing import run_session_store_conformance

from imp.providers.claude_code_session_store import (
    ImpSessionStore,
    resume_session_id,
    session_key,
    session_project_key,
    session_store_for,
)


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


def test_append_dedups_entries_by_uuid(tmp_path: Path) -> None:
    store = ImpSessionStore(tmp_path)
    key = _main_key()
    entry = {"type": "assistant", "uuid": "dup", "message": {"n": 1}}
    anyio.run(store.append, key, [entry])
    anyio.run(store.append, key, [entry])  # redelivered batch (idempotency key)

    loaded = anyio.run(store.load, key)
    assert loaded == [entry]


def test_append_keeps_entries_without_uuid(tmp_path: Path) -> None:
    store = ImpSessionStore(tmp_path)
    key = _main_key()
    no_uuid = {"type": "ai-title", "title": "x"}
    anyio.run(store.append, key, [no_uuid])
    anyio.run(store.append, key, [no_uuid])

    loaded = anyio.run(store.load, key)
    assert loaded == [no_uuid, no_uuid]


def test_subpath_key_routes_to_separate_file(tmp_path: Path) -> None:
    store = ImpSessionStore(tmp_path)
    main = _main_key()
    sub = {**main, "subpath": "subagents/agent-1"}
    anyio.run(store.append, main, [{"type": "user", "uuid": "m"}])
    anyio.run(store.append, sub, [{"type": "user", "uuid": "a"}])

    assert anyio.run(store.load, main) == [{"type": "user", "uuid": "m"}]
    assert anyio.run(store.load, sub) == [{"type": "user", "uuid": "a"}]


def test_append_dedups_within_a_single_batch(tmp_path: Path) -> None:
    store = ImpSessionStore(tmp_path)
    key = _main_key()
    entry = {"type": "assistant", "uuid": "dup", "message": {"n": 1}}
    anyio.run(store.append, key, [entry, entry])

    assert anyio.run(store.load, key) == [entry]


def test_list_subkeys_returns_subagent_subpaths_not_main(tmp_path: Path) -> None:
    store = ImpSessionStore(tmp_path)
    main = _main_key()
    sub_a = {**main, "subpath": "subagents/agent-1"}
    sub_b = {**main, "subpath": "subagents/agent-2"}
    anyio.run(store.append, main, [{"type": "user", "uuid": "m"}])
    anyio.run(store.append, sub_a, [{"type": "user", "uuid": "a"}])
    anyio.run(store.append, sub_b, [{"type": "user", "uuid": "b"}])

    subkeys = anyio.run(
        store.list_subkeys, {"project_key": main["project_key"],
                             "session_id": main["session_id"]})
    assert sorted(subkeys) == ["subagents/agent-1", "subagents/agent-2"]


def test_list_subkeys_empty_for_unknown_session(tmp_path: Path) -> None:
    store = ImpSessionStore(tmp_path)
    assert anyio.run(
        store.list_subkeys,
        {"project_key": "/home/u/proj", "session_id": "nope"}) == []


def test_list_subkeys_includes_nested_file_named_main(tmp_path: Path) -> None:
    store = ImpSessionStore(tmp_path)
    main = _main_key()
    nested = {**main, "subpath": "subagents/main"}
    anyio.run(store.append, main, [{"type": "user", "uuid": "m"}])
    anyio.run(store.append, nested, [{"type": "user", "uuid": "n"}])

    subkeys = anyio.run(
        store.list_subkeys, {"project_key": main["project_key"],
                             "session_id": main["session_id"]})
    assert subkeys == ["subagents/main"]


def test_delete_main_key_cascades_to_subagents(tmp_path: Path) -> None:
    store = ImpSessionStore(tmp_path)
    main = _main_key()
    sub = {**main, "subpath": "subagents/agent-1"}
    anyio.run(store.append, main, [{"type": "user", "uuid": "m"}])
    anyio.run(store.append, sub, [{"type": "user", "uuid": "a"}])

    anyio.run(store.delete, main)

    assert anyio.run(store.load, main) is None
    assert anyio.run(store.load, sub) is None


def test_delete_subpath_removes_only_that_entry(tmp_path: Path) -> None:
    store = ImpSessionStore(tmp_path)
    main = _main_key()
    sub = {**main, "subpath": "subagents/agent-1"}
    anyio.run(store.append, main, [{"type": "user", "uuid": "m"}])
    anyio.run(store.append, sub, [{"type": "user", "uuid": "a"}])

    anyio.run(store.delete, sub)

    assert anyio.run(store.load, sub) is None
    assert anyio.run(store.load, main) == [{"type": "user", "uuid": "m"}]


def test_subpath_traversal_stays_under_root(tmp_path: Path) -> None:
    store = ImpSessionStore(tmp_path)
    key = {**_main_key(), "subpath": "../../escape"}
    anyio.run(store.append, key, [{"type": "user", "uuid": "t"}])
    written = list(tmp_path.rglob("*.jsonl"))
    assert written, "expected a JSONL file to be written"
    for p in written:
        assert tmp_path in p.parents


def test_subpath_leading_slash_stays_under_root(tmp_path: Path) -> None:
    store = ImpSessionStore(tmp_path)
    key = {**_main_key(), "subpath": "/escape"}
    anyio.run(store.append, key, [{"type": "user", "uuid": "t"}])
    written = list(tmp_path.rglob("*.jsonl"))
    assert written, "expected a JSONL file to be written"
    for p in written:
        assert tmp_path in p.parents


def test_passes_sdk_session_store_conformance(tmp_path: Path) -> None:
    anyio.run(
        run_session_store_conformance,
        lambda: ImpSessionStore(Path(tempfile.mkdtemp(dir=tmp_path))),
    )


def test_resume_session_id_is_stable_and_a_valid_uuid() -> None:
    first = resume_session_id("step:plan/plan-writer")
    second = resume_session_id("step:plan/plan-writer")
    assert first == second
    # Parses as a UUID (SDK rejects a non-UUID resume id), so validity is load-bearing.
    assert str(uuid.UUID(first)) == first


def test_resume_session_id_distinct_per_agent() -> None:
    assert resume_session_id("step:plan/a") != resume_session_id("step:plan/b")


def test_session_key_uses_sdk_project_key_and_derived_id(tmp_path: Path) -> None:
    from claude_agent_sdk import project_key_for_directory

    key = session_key(tmp_path, "step:plan/plan-writer")
    # project_key MUST equal the SDK's own derivation, or native resume never finds it.
    assert key["project_key"] == project_key_for_directory(tmp_path)
    assert key["session_id"] == resume_session_id("step:plan/plan-writer")


def test_session_project_key_is_the_sdk_helper(tmp_path: Path) -> None:
    from claude_agent_sdk import project_key_for_directory

    assert session_project_key(tmp_path) == project_key_for_directory(tmp_path)


def test_session_store_for_writes_under_claude_session_subdir(
    tmp_path: Path,
) -> None:
    store = session_store_for(tmp_path)
    assert isinstance(store, ImpSessionStore)
    anyio.run(
        store.append,
        session_key(tmp_path, "step:plan/plan-writer"),
        [{"type": "user", "uuid": "x"}],
    )
    written = list((tmp_path / "claude_session").rglob("*.jsonl"))
    assert written, "append must land under <transcript_dir>/claude_session/"
