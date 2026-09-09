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


_NOTE = "# Handoff\nGoal: land the parser\nRemains: wire `--json` output\n"

_MARKER = ("--- HANDOFF NOTE FROM THE PREVIOUS ATTEMPT "
           "(verbatim, to end of message) ---")


def test_seed_with_a_note_inlines_it_verbatim_and_last() -> None:
    # The note is the primary source and arrives IN the message; the step-scoped
    # events query is the deeper-detail fallback.
    seed = reincarnation_seed(
        handoff_text=_NOTE, quest_id="q7", step_name="coder")
    assert "- Primary: the previous attempt's handoff note" in seed
    assert "- Deeper detail: `imp events q7 --step coder --limit N`" in seed
    assert "- Orientation only: `imp events q7 --limit N`" in seed
    assert seed.count(_MARKER) == 1
    assert seed.endswith(f"{_MARKER}\n\n{_NOTE}")


def test_seed_without_a_note_makes_the_step_query_primary() -> None:
    seed = reincarnation_seed(
        handoff_text=None, quest_id="q7", step_name="coder")
    assert "- Primary: `imp events q7 --step coder --limit N`" in seed
    assert "It left no handoff note, so this is the record of what it did." in seed
    assert "- Orientation only: `imp events q7 --limit N`" in seed
    assert "HANDOFF NOTE FROM THE PREVIOUS ATTEMPT" not in seed
    assert "Deeper detail" not in seed


def test_seed_names_no_sandbox_masked_path_in_either_shape() -> None:
    # The reported defect: everything under <quest_dir>/steps/ is masked by
    # resolve_policy, so no seed variant may point the reborn session at one.
    for text in (_NOTE, None):
        seed = reincarnation_seed(
            handoff_text=text, quest_id="q7", step_name="coder")
        assert "/steps/" not in seed
        assert "transcript.jsonl" not in seed
        assert ".handoff.md" not in seed
        assert "read_file" not in seed


def test_seed_shapes_share_the_opening_and_orientation_lines() -> None:
    with_note = reincarnation_seed(
        handoff_text=_NOTE, quest_id="q7", step_name="coder").splitlines()
    without = reincarnation_seed(
        handoff_text=None, quest_id="q7", step_name="coder").splitlines()
    assert with_note[0].startswith("You are a reincarnated session:")
    assert with_note[0] == without[0]
    assert with_note[3] == without[2]        # the orientation bullet, verbatim


def test_handoff_path_is_sibling_of_transcript_dir() -> None:
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
