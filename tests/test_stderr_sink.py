# tests/test_stderr_sink.py
from __future__ import annotations

from pathlib import Path

from imp.providers.stderr_sink import StderrSink, current_stderr_sink


def test_feed_writes_file_and_tail(tmp_path: Path) -> None:
    sink = StderrSink(tmp_path / "stderr.log")
    sink.feed("first line\n")
    sink.feed("second line\n")
    sink.close()

    on_disk = (tmp_path / "stderr.log").read_text()
    assert "first line" in on_disk
    assert "second line" in on_disk
    assert sink.tail() == "first line\nsecond line\n"


def test_tail_is_none_when_nothing_fed(tmp_path: Path) -> None:
    sink = StderrSink(tmp_path / "stderr.log")
    assert sink.tail() is None


def test_tail_is_bounded(tmp_path: Path) -> None:
    sink = StderrSink(tmp_path / "stderr.log")
    sink.feed("x" * 5000)
    tail = sink.tail()
    assert tail is not None
    assert len(tail.encode("utf-8")) <= 2048


def test_file_cap_truncates_with_marker(tmp_path: Path) -> None:
    sink = StderrSink(tmp_path / "stderr.log")
    # Feed past the 1 MiB cap.
    sink.feed("a" * (1_048_576 + 100))
    sink.feed("more after cap")
    sink.close()
    data = (tmp_path / "stderr.log").read_text()
    assert "[stderr truncated]" in data
    assert len(data.encode("utf-8")) < 1_048_576 + 1000  # capped, not unbounded


def test_feed_tolerates_unwritable_path_but_keeps_tail(tmp_path: Path) -> None:
    # A directory path can't be opened as a file → write fails, tail still works.
    bad = tmp_path / "subdir"
    bad.mkdir()
    sink = StderrSink(bad)  # parent exists but target is a directory
    sink.feed("boom happened\n")
    assert sink.tail() == "boom happened\n"
    sink.close()


def test_feed_after_close_updates_tail_without_reopening(tmp_path: Path) -> None:
    log = tmp_path / "stderr.log"
    sink = StderrSink(log)
    sink.feed("before close\n")
    sink.close()
    sink.feed("after close\n")  # the codex floor feeds str(exc) post-close
    # Tail reflects the post-close feed...
    assert sink.tail() == "before close\nafter close\n"
    # ...but the file was not reopened, so it holds only the pre-close content.
    assert log.read_text() == "before close\n"


def test_contextvar_default_is_none() -> None:
    assert current_stderr_sink.get() is None
