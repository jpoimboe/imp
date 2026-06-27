from pathlib import Path

from imp.engine.reincarnation import stash_transcript


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
