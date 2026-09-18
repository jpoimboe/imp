from pathlib import Path

import pytest

from imp.agents.runner import Message, TextBlock
from imp.engine.reincarnation import (
    handoff_instruction,
    handoff_path_for,
    last_user_message,
    provider_path_for,
    read_session_owner,
    reincarnation_seed,
    stash_session,
    write_handoff_note,
    write_session_owner,
)
from imp.engine.resume_state import (
    InvalidResumeState,
    NativeResumeState,
    encode_native_resume_state,
)
from imp.engine.state import InMemoryEventLog


def _seed(transcript_dir: Path, body: str = '{"k": 1}\n') -> None:
    transcript_dir.mkdir(parents=True, exist_ok=True)
    (transcript_dir / "transcript.jsonl").write_text(body)


def test_stash_moves_nonempty_session_aside(tmp_path: Path) -> None:
    tx = tmp_path / "steps" / "design" / "spec"
    _seed(tx, '{"turn": 1}\n')
    dest = stash_session(tx)
    assert dest == tmp_path / "steps" / "design" / "spec.attempt-1"
    assert not tx.exists()                                   # moved aside
    assert (dest / "transcript.jsonl").read_text() == '{"turn": 1}\n'


def test_stash_picks_next_free_index(tmp_path: Path) -> None:
    tx = tmp_path / "steps" / "design" / "spec"
    _seed(tx)
    stash_session(tx)            # -> spec.attempt-1
    _seed(tx)                       # caller would normally mkdir afresh
    dest = stash_session(tx)     # -> spec.attempt-2
    assert dest == tmp_path / "steps" / "design" / "spec.attempt-2"
    assert (tmp_path / "steps" / "design" / "spec.attempt-1").exists()


def test_stash_returns_none_when_nothing_to_stash(tmp_path: Path) -> None:
    missing = tmp_path / "steps" / "design" / "spec"
    assert stash_session(missing) is None                # absent dir
    empty = tmp_path / "steps" / "design" / "other"
    empty.mkdir(parents=True)
    assert stash_session(empty) is None                  # no transcript.jsonl
    (empty / "transcript.jsonl").write_text("")
    assert stash_session(empty) is None                  # empty transcript


_NOTE = "# Handoff\nGoal: land the parser\nRemains: wire `--json` output\n"

_MARKER = ("--- HANDOFF NOTE FROM THE PREVIOUS ATTEMPT "
           "(verbatim, to end of message) ---")

_USER_MSG = "Two questions: does the daemon retry, and where does it log?"

_USER_OPEN = "--- THE USER'S MOST RECENT MESSAGE (verbatim) ---"

_USER_CLOSE = "--- END OF THE USER'S MOST RECENT MESSAGE ---"

_OWED_BULLET = (
    "- Owed to the user: the previous attempt may have stopped mid-reply, so "
    "the last thing on the user's screen can be a fragment. The user's most "
    "recent message is reproduced between the markers below; check whether it "
    "has been answered in prose the user can see, and answer it yourself if it "
    "has not.")


def test_seed_with_a_note_inlines_it_verbatim_and_last() -> None:
    # The note is the primary source and arrives IN the message; the step-scoped
    # events query is the deeper-detail fallback.
    seed = reincarnation_seed(
        handoff_text=_NOTE, quest_id="q7", step_name="coder",
        last_user_text=None)
    assert "- Primary: the previous attempt's handoff note" in seed
    assert "- Deeper detail: `imp events q7 --step coder --limit N`" in seed
    assert "- Orientation only: `imp events q7 --limit N`" in seed
    assert seed.count(_MARKER) == 1
    assert seed.endswith(f"{_MARKER}\n\n{_NOTE}")


def test_seed_without_a_note_makes_the_step_query_primary() -> None:
    seed = reincarnation_seed(
        handoff_text=None, quest_id="q7", step_name="coder",
        last_user_text=None)
    assert "- Primary: `imp events q7 --step coder --limit N`" in seed
    assert "It left no handoff note, so this is the record of what it did." in seed
    assert "- Orientation only: `imp events q7 --limit N`" in seed
    assert "HANDOFF NOTE FROM THE PREVIOUS ATTEMPT" not in seed
    assert "Deeper detail" not in seed


def test_the_users_message_is_carried_before_the_note() -> None:
    seed = reincarnation_seed(
        handoff_text=_NOTE, quest_id="q7", step_name="coder",
        last_user_text=_USER_MSG)
    assert _OWED_BULLET in seed
    assert seed.count(_USER_MSG) == 1
    assert seed.count(_USER_OPEN) == 1
    assert seed.split(_USER_OPEN, 1)[1].startswith(
        f"\n\n{_USER_MSG}\n\n{_USER_CLOSE}")
    # The note's marker promises "to end of message", so the user block precedes it.
    assert seed.index(_USER_OPEN) < seed.index(_MARKER)
    assert seed.endswith(f"{_MARKER}\n\n{_NOTE}")


def test_the_note_less_seed_ends_with_the_users_message() -> None:
    seed = reincarnation_seed(
        handoff_text=None, quest_id="q7", step_name="coder",
        last_user_text=_USER_MSG)
    assert _OWED_BULLET in seed
    assert "HANDOFF NOTE FROM THE PREVIOUS ATTEMPT" not in seed
    assert seed.endswith(f"{_USER_OPEN}\n\n{_USER_MSG}\n\n{_USER_CLOSE}")


def test_no_user_message_adds_neither_bullet_nor_block() -> None:
    seed = reincarnation_seed(
        handoff_text=_NOTE, quest_id="q7", step_name="coder",
        last_user_text=None)
    assert "Owed to the user" not in seed
    assert _USER_OPEN not in seed
    assert _USER_CLOSE not in seed
    assert seed.endswith(f"{_MARKER}\n\n{_NOTE}")


def test_seed_names_no_sandbox_masked_path_in_either_shape() -> None:
    # The reported defect: everything under <quest_dir>/steps/ is masked by
    # resolve_policy, so no seed variant may point the reborn session at one.
    for text in (_NOTE, None):
        for user_text in (None, _USER_MSG):
            seed = reincarnation_seed(
                handoff_text=text, quest_id="q7", step_name="coder",
                last_user_text=user_text)
            assert "/steps/" not in seed
            assert "transcript.jsonl" not in seed
            assert ".handoff.md" not in seed
            assert "read_file" not in seed


def test_seed_shapes_share_the_opening_and_orientation_lines() -> None:
    with_note = reincarnation_seed(
        handoff_text=_NOTE, quest_id="q7", step_name="coder",
        last_user_text=None).splitlines()
    without = reincarnation_seed(
        handoff_text=None, quest_id="q7", step_name="coder",
        last_user_text=None).splitlines()
    assert with_note[0].startswith("You are a reincarnated session:")
    assert with_note[0] == without[0]
    assert with_note[3] == without[2]        # the orientation bullet, verbatim


def test_handoff_path_is_sibling_of_session_dir() -> None:
    # A sibling (not a child) so it survives the fresh-entry wipe of the dir.
    p = handoff_path_for(Path("/x/steps/impl/coder"))
    assert p == Path("/x/steps/impl/coder.handoff.md")


def test_handoff_instruction_tells_agent_to_reply_and_stop() -> None:
    instr = handoff_instruction()
    assert "write_file" not in instr
    assert "context limit" in instr
    assert "stop" in instr.lower()
    # Must direct the agent to emit the handoff as its final reply text.
    assert "final reply" in instr.lower()
    assert "remains" in instr.lower()
    # Must mention file:line citation style.
    assert "file:line" in instr


def test_write_handoff_note_writes_nonempty_text(tmp_path: Path) -> None:
    note = tmp_path / "coder.handoff.md"
    text = "Goal: finish the thing\nProgress: 50%\n"
    result = write_handoff_note(note, text)
    assert result is True
    assert note.read_text() == text


def test_write_handoff_note_empty_string_writes_nothing(tmp_path: Path) -> None:
    note = tmp_path / "coder.handoff.md"
    result = write_handoff_note(note, "")
    assert result is False
    assert not note.exists()


def test_write_handoff_note_whitespace_only_writes_nothing(tmp_path: Path) -> None:
    note = tmp_path / "coder.handoff.md"
    result = write_handoff_note(note, "   \n\t ")
    assert result is False
    assert not note.exists()


def test_session_owner_lives_inside_the_dir_and_travels_with_it(
        tmp_path: Path) -> None:
    # INSIDE (not a sibling, unlike the handoff note): the label describes the
    # transcript, so it must follow it into a stash and vanish with a wipe.
    tx = tmp_path / "steps" / "impl" / "coder"
    _seed(tx)

    write_session_owner(tx, "claude")

    assert provider_path_for(tx) == tx / "provider"
    assert read_session_owner(tx) == "claude"
    dest = stash_session(tx)
    assert dest is not None
    assert read_session_owner(dest) == "claude"
    assert not tx.exists()


def test_an_unlabelled_session_dir_has_no_owner(tmp_path: Path) -> None:
    # No label and a blank label both read as None, which never matches a
    # provider name — the safe direction for the dispatch-time check.
    tx = tmp_path / "steps" / "impl" / "coder"
    _seed(tx)
    assert read_session_owner(tx) is None

    (tx / "provider").write_text("  \n")

    assert read_session_owner(tx) is None


_CONTEXT_OPENING = (
    "You are a reincarnated session: the previous attempt approached its "
    "context limit and was restarted fresh to keep working without a silent "
    "compaction. Pick up where it left off using the recovery sources below.")

_SWITCH_OPENING = (
    "You are a reincarnated session: the previous attempt ran on a different "
    "provider and was switched at the user's request, not because anything went "
    "wrong. Pick up where it left off using the recovery sources below.")


def test_the_switch_cause_opens_with_the_pinned_sentence() -> None:
    seed = reincarnation_seed(
        handoff_text=None, quest_id="q7", step_name="coder",
        last_user_text=None, cause="provider_switch")
    assert seed.splitlines()[0] == _SWITCH_OPENING
    assert "context limit" not in seed


def test_the_default_cause_still_opens_with_the_context_sentence() -> None:
    # The Tier A/B callers pass no cause and must be byte-identical to today.
    seed = reincarnation_seed(
        handoff_text=None, quest_id="q7", step_name="coder", last_user_text=None)
    assert seed.splitlines()[0] == _CONTEXT_OPENING
    assert "switched at the user's request" not in seed


def test_only_the_opening_sentence_differs_between_causes() -> None:
    # The recovery sources are the same record whatever ended the last session.
    switched = reincarnation_seed(
        handoff_text=None, quest_id="q7", step_name="coder",
        last_user_text=None, cause="provider_switch").splitlines()
    context = reincarnation_seed(
        handoff_text=None, quest_id="q7", step_name="coder",
        last_user_text=None).splitlines()
    assert switched[1:] == context[1:]
    assert switched[0] != context[0]


def test_stash_moves_a_native_only_session_aside(tmp_path: Path) -> None:
    session = tmp_path / "steps" / "impl" / "coder"
    session.mkdir(parents=True)
    manifest = encode_native_resume_state(NativeResumeState(provider="codex", state={}))
    (session / "resume-state.json").write_bytes(manifest)

    dest = stash_session(session)

    assert dest == tmp_path / "steps" / "impl" / "coder.attempt-1"
    assert not session.exists()
    assert (dest / "resume-state.json").read_bytes() == manifest


def test_malformed_manifest_prevents_session_stash(tmp_path: Path) -> None:
    session = tmp_path / "steps" / "impl" / "coder"
    _seed(session)
    (session / "resume-state.json").write_bytes(b"{")
    before = {
        path.relative_to(session): path.read_bytes()
        for path in session.rglob("*") if path.is_file()
    }

    with pytest.raises(InvalidResumeState):
        stash_session(session)

    after = {
        path.relative_to(session): path.read_bytes()
        for path in session.rglob("*") if path.is_file()
    }
    assert after == before
    assert not (session.parent / "coder.attempt-1").exists()


def _user(log: InMemoryEventLog, *texts: str, subtype: str = "reply") -> None:
    log.append_user_message(
        agent_id="step:design/spec",
        message=Message(role="user",
                        content=tuple(TextBlock(text=t) for t in texts)),
        subtype=subtype)


def _step_started(log: InMemoryEventLog, step: str) -> None:
    log.append(agent_id=None, type="step_started",
               payload={"step": step, "kind": "agent", "phase": "design"})


def test_the_most_recent_reply_is_the_one_carried() -> None:
    log = InMemoryEventLog()
    _user(log, "what does the daemon retry?")
    _user(log, "and where does it log?")
    assert last_user_message(log, step_name="spec") == "and where does it log?"


def test_a_trailing_interject_is_carried_like_a_reply() -> None:
    # A steering interjection lost to a handoff is the same failure as a lost reply.
    log = InMemoryEventLog()
    _user(log, "and where does it log?")
    _user(log, "stop and reconsider", subtype="interject")
    assert last_user_message(log, step_name="spec") == "stop and reconsider"


def test_the_quest_opening_intent_is_never_carried() -> None:
    # The reborn agent's template already renders run.opening; quoting the intent
    # back would duplicate it. An empty log has nothing to carry either.
    log = InMemoryEventLog()
    _user(log, "build me a parser", subtype="intent")
    assert last_user_message(log, step_name="spec") is None
    assert last_user_message(InMemoryEventLog(), step_name="spec") is None


def test_a_different_steps_start_ends_the_engagement() -> None:
    # A stale threshold reply must not leak into an unrelated later step's seed.
    log = InMemoryEventLog()
    _user(log, "and where does it log?")
    _step_started(log, "plan")
    assert last_user_message(log, step_name="spec") is None


def test_the_same_steps_re_entry_keeps_the_message() -> None:
    # Exactly the reincarnation re-entry the message must span.
    log = InMemoryEventLog()
    _user(log, "and where does it log?")
    _step_started(log, "spec")
    assert last_user_message(log, step_name="spec") == "and where does it log?"


def test_a_whitespace_only_message_carries_nothing() -> None:
    log = InMemoryEventLog()
    _user(log, "and where does it log?")
    _user(log, "   \n\t ")
    assert last_user_message(log, step_name="spec") is None
    _user(log, "")
    assert last_user_message(log, step_name="spec") is None
    _user(log)
    assert last_user_message(log, step_name="spec") is None


def test_the_text_blocks_of_one_message_join_in_order() -> None:
    log = InMemoryEventLog()
    _user(log, "first line\n", "second line")
    assert last_user_message(log, step_name="spec") == "first line\nsecond line"
