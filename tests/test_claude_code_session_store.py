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
    store_has_conversation_body,
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


def test_store_has_conversation_body_distinguishes_stub_from_body() -> None:
    # Empty / missing store → no resumable body.
    assert store_has_conversation_body(None) is False
    assert store_has_conversation_body([]) is False

    # ai-title stub only (CC 2.x writes this even under suppression) → no body.
    assert store_has_conversation_body([{"type": "ai-title", "title": "x"}]) is False

    # A real conversation entry alongside the stub → resumable body present.
    assert store_has_conversation_body(
        [{"type": "ai-title", "title": "x"}, {"type": "assistant", "uuid": "a"}]
    ) is True

    # Body-only with no ai-title at all → still a healthy real session.
    assert store_has_conversation_body([{"type": "assistant", "uuid": "a"}]) is True

    # Non-dict elements are tolerated by the isinstance guard; alone they yield False.
    assert store_has_conversation_body([None, "string", 42]) is False


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


def test_message_to_session_records_chains_and_shapes() -> None:
    from imp.agents.runner import (
        Message,
        TextBlock,
        ToolResultBlock,
        ToolUseBlock,
    )
    from imp.providers.claude_code_session_store import message_to_session_records

    messages = [
        Message(role="assistant", content=(
            TextBlock(text="reading"),
            ToolUseBlock(id="t0", name="mcp__imp__read_file", input={"path": "spec.md"}),
        )),
        Message(role="user", content=(
            ToolResultBlock(tool_use_id="t0", content="BODY", is_error=False),)),
    ]
    recs = message_to_session_records(
        messages, parent_uuid="P", session_id="sid", cwd="/proj",
        model="claude-sonnet-4-5", start_index=2)

    assert len(recs) == 2
    assert recs[0]["type"] == "assistant"
    assert recs[0]["parentUuid"] == "P"
    assert recs[0]["sessionId"] == "sid"
    assert recs[0]["cwd"] == "/proj"
    assert recs[0]["message"]["role"] == "assistant"
    assert recs[0]["message"]["model"] == "claude-sonnet-4-5"
    msg_id = recs[0]["message"]["id"]
    assert msg_id.startswith("msg_")
    assert msg_id[4:] == recs[0]["uuid"].replace("-", "")[:24]
    tool_use = recs[0]["message"]["content"][1]
    assert tool_use == {"type": "tool_use", "id": "t0",
                        "name": "mcp__imp__read_file", "input": {"path": "spec.md"}}
    assert recs[1]["type"] == "user"
    assert recs[1]["parentUuid"] == recs[0]["uuid"]
    assert recs[1]["message"]["content"][0] == {
        "type": "tool_result", "tool_use_id": "t0",
        "content": "BODY", "is_error": False}


def test_message_to_session_records_uuids_are_deterministic(tmp_path: Path) -> None:
    from imp.agents.runner import Message, TextBlock
    from imp.providers.claude_code_session_store import message_to_session_records

    messages = [Message(role="assistant", content=(TextBlock(text="hi"),))]
    a = message_to_session_records(
        messages, parent_uuid=None, session_id="sid", cwd="/p",
        model="m", start_index=5)
    b = message_to_session_records(
        messages, parent_uuid=None, session_id="sid", cwd="/p",
        model="m", start_index=5)
    assert [r["uuid"] for r in a] == [r["uuid"] for r in b]
    store = ImpSessionStore(tmp_path)
    key = _main_key()
    anyio.run(store.append, key, a)
    anyio.run(store.append, key, b)
    assert len(anyio.run(store.load, key)) == 1


def test_strip_trailing_error_turn_drops_session_limit_turn() -> None:
    from imp.agents.runner import Message, TextBlock
    from imp.providers.claude_code_session_store import (
        message_to_session_records,
        strip_trailing_error_turn,
    )

    good = message_to_session_records(
        [Message(role="assistant", content=(TextBlock(text="done"),))],
        parent_uuid=None, session_id="sid", cwd="/p", model="m", start_index=0)
    bad = message_to_session_records(
        [Message(role="assistant", content=(
            TextBlock(text="You've hit your session limit · resets 1am (America/Los_Angeles)"),))],
        parent_uuid=good[-1]["uuid"], session_id="sid", cwd="/p", model="m",
        start_index=1)
    entries = good + bad

    assert strip_trailing_error_turn(entries) == good


def test_strip_trailing_error_turn_drops_is_api_error_flag() -> None:
    from imp.providers.claude_code_session_store import strip_trailing_error_turn

    entries = [
        {"type": "assistant", "uuid": "a", "message": {"role": "assistant"}},
        {"type": "assistant", "uuid": "b", "isApiErrorMessage": True,
         "message": {"role": "assistant", "content": []}},
    ]
    assert strip_trailing_error_turn(entries) == entries[:1]


def test_strip_trailing_error_turn_drops_nested_message_api_error_flag() -> None:
    from imp.providers.claude_code_session_store import strip_trailing_error_turn

    entries = [
        {"type": "assistant", "uuid": "a", "message": {"role": "assistant"}},
        {"type": "assistant", "uuid": "b",
         "message": {"role": "assistant", "isApiErrorMessage": True, "content": []}},
    ]
    assert strip_trailing_error_turn(entries) == entries[:1]


def test_strip_trailing_error_turn_drops_string_content_session_limit() -> None:
    from imp.providers.claude_code_session_store import strip_trailing_error_turn

    entries = [
        {"type": "assistant", "uuid": "a",
         "message": {"role": "assistant", "content": "all good"}},
        {"type": "assistant", "uuid": "b",
         "message": {"role": "assistant",
                     "content": "You've hit your session limit, try later"}},
    ]
    assert strip_trailing_error_turn(entries) == entries[:1]


def test_strip_trailing_error_turn_keeps_normal_tail() -> None:
    from imp.providers.claude_code_session_store import strip_trailing_error_turn

    entries = [{"type": "assistant", "uuid": "a",
                "message": {"role": "assistant", "content": [
                    {"type": "text", "text": "all good"}]}}]
    assert strip_trailing_error_turn(entries) == entries
    assert strip_trailing_error_turn([]) == []


def _three_message_transcript() -> list:
    from imp.agents.runner import (
        Message,
        TextBlock,
        ToolResultBlock,
        ToolUseBlock,
    )
    return [
        Message(role="user", content=(TextBlock(text="go"),)),
        Message(role="assistant", content=(
            ToolUseBlock(id="t0", name="mcp__imp__read_file", input={"path": "s"}),)),
        Message(role="user", content=(
            ToolResultBlock(tool_use_id="t0", content="BODY", is_error=False),)),
    ]


def test_reconcile_store_tail_appends_missing_trailing_turn() -> None:
    from imp.providers.claude_code_session_store import (
        message_to_session_records,
        reconcile_store_tail,
    )

    messages = _three_message_transcript()
    store_body = message_to_session_records(
        messages[:2], parent_uuid=None, session_id="sid", cwd="/p",
        model="m", start_index=0)

    out = reconcile_store_tail(
        messages, store_body, session_id="sid", cwd="/p", model="m")

    assert len(out) == 3
    assert out[:2] == store_body
    assert out[2]["type"] == "user"
    assert out[2]["parentUuid"] == store_body[-1]["uuid"]
    assert out[2]["message"]["content"][0]["type"] == "tool_result"


def test_reconcile_store_tail_strips_error_turn_then_resynthesizes() -> None:
    from imp.providers.claude_code_session_store import (
        message_to_session_records,
        reconcile_store_tail,
    )

    messages = _three_message_transcript()
    store_body = message_to_session_records(
        messages[:2], parent_uuid=None, session_id="sid", cwd="/p",
        model="m", start_index=0)
    from imp.agents.runner import Message, TextBlock
    err = message_to_session_records(
        [Message(role="assistant", content=(
            TextBlock(text="You've hit your session limit (America/Los_Angeles)"),))],
        parent_uuid=store_body[-1]["uuid"], session_id="sid", cwd="/p",
        model="m", start_index=2)
    store_with_error = store_body + err

    out = reconcile_store_tail(
        messages, store_with_error, session_id="sid", cwd="/p", model="m")

    assert len(out) == 3
    assert out[:2] == store_body
    assert out[2]["message"]["content"][0]["type"] == "tool_result"


def test_reconcile_store_tail_is_idempotent_when_complete() -> None:
    from imp.providers.claude_code_session_store import (
        message_to_session_records,
        reconcile_store_tail,
    )

    messages = _three_message_transcript()
    full = message_to_session_records(
        messages, parent_uuid=None, session_id="sid", cwd="/p",
        model="m", start_index=0)
    out = reconcile_store_tail(
        messages, full, session_id="sid", cwd="/p", model="m")
    assert [r["uuid"] for r in out] == [r["uuid"] for r in full]
    assert len(out) == 3
