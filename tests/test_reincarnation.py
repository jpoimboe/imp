from pathlib import Path

from imp.engine.reincarnation import (
    handoff_instruction,
    handoff_path_for,
    reincarnation_seed,
    stash_transcript,
    write_handoff_note,
)


def _seed(transcript_dir: Path, body: str = '{"k": 1}\n') -> None:
    transcript_dir.mkdir(parents=True, exist_ok=True)
    (transcript_dir / "transcript.jsonl").write_text(body)


def test_stash_moves_nonempty_transcript_aside(tmp_path: Path) -> None:
    tx = tmp_path / "steps" / "design" / "spec"
    _seed(tx, '{"turn": 1}\n')
    dest = stash_transcript(tx)
    assert dest == tmp_path / "steps" / "design" / "spec.attempt-1"
    assert not tx.exists()                                   # moved aside
    assert (dest / "transcript.jsonl").read_text() == '{"turn": 1}\n'


def test_stash_picks_next_free_index(tmp_path: Path) -> None:
    tx = tmp_path / "steps" / "design" / "spec"
    _seed(tx)
    stash_transcript(tx)            # -> spec.attempt-1
    _seed(tx)                       # caller would normally mkdir afresh
    dest = stash_transcript(tx)     # -> spec.attempt-2
    assert dest == tmp_path / "steps" / "design" / "spec.attempt-2"
    assert (tmp_path / "steps" / "design" / "spec.attempt-1").exists()


def test_stash_returns_none_when_nothing_to_stash(tmp_path: Path) -> None:
    missing = tmp_path / "steps" / "design" / "spec"
    assert stash_transcript(missing) is None                # absent dir
    empty = tmp_path / "steps" / "design" / "other"
    empty.mkdir(parents=True)
    assert stash_transcript(empty) is None                  # no transcript.jsonl
    (empty / "transcript.jsonl").write_text("")
    assert stash_transcript(empty) is None                  # empty transcript


def test_seed_tier_b_points_at_stashed_transcript_and_events_query() -> None:
    # Tier B: no handoff was captured (hard error). The seed points the reborn
    # session at the stashed transcript and at imp events for orientation.
    seed = reincarnation_seed(
        stashed_dir=Path("/x/steps/impl/coder.attempt-1"), handoff_path=None,
        quest_id="q7")
    assert "/x/steps/impl/coder.attempt-1/transcript.jsonl" in seed
    assert "imp events q7 --limit N" in seed
    assert "handoff" not in seed.lower()
    # No handoff was written, so the seed must not refer to a summary above it.
    assert "summary above" not in seed


def test_seed_tier_a_names_handoff_as_primary() -> None:
    # Tier A (built now for the later proactive-handoff lap): the handoff note is
    # the primary recovery source; the stashed transcript is the deeper-dig source.
    seed = reincarnation_seed(
        stashed_dir=Path("/x/a-1"), handoff_path=Path("/x/handoff.md"),
        quest_id="q7")
    assert "/x/handoff.md" in seed
    assert "- Primary: read the handoff note" in seed
    assert "/x/a-1/transcript.jsonl" in seed
    assert "summary above" in seed


def test_seed_drops_false_claim_points_at_bounded_query_and_names_transcript_primary() -> None:
    # Part 3: the orientation pointer must no longer claim imp events excludes
    # this step's own tool calls (it doesn't), must point at a BOUNDED imp events
    # id --limit N call, and the stashed transcript must be labelled the primary
    # detail source.
    seed = reincarnation_seed(
        stashed_dir=Path("/x/steps/impl/coder.attempt-1"), handoff_path=None,
        quest_id="q7")
    assert "this step's own tool calls" not in seed
    assert "imp events q7 --limit N" in seed
    assert "Primary detail source" in seed
    assert "/x/steps/impl/coder.attempt-1/transcript.jsonl" in seed


def test_seed_frames_events_query_as_provenance_not_current_state() -> None:
    seed = reincarnation_seed(stashed_dir=None, handoff_path=None, quest_id="q7")
    assert "provenance" in seed
    assert "not for current state" in seed
    assert "imp events q7 --limit N" in seed


def test_seed_degrades_to_orientation_only_when_nothing_stashed() -> None:
    seed = reincarnation_seed(stashed_dir=None, handoff_path=None, quest_id="q7")
    assert seed                                  # non-empty
    assert "imp events" in seed
    assert "transcript.jsonl" not in seed


def test_handoff_path_is_sibling_of_transcript_dir() -> None:
    # A sibling (not a child) so it survives the fresh-entry wipe of the dir.
    p = handoff_path_for(Path("/x/steps/impl/coder"))
    assert p == Path("/x/steps/impl/coder.handoff.md")


def test_handoff_instruction_tells_agent_to_reply_and_stop() -> None:
    instr = handoff_instruction(Path("/x/steps/impl/coder.handoff.md"))
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
