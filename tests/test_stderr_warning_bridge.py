# tests/test_stderr_warning_bridge.py
from __future__ import annotations

from pathlib import Path

from imp.engine.state import InMemoryEventLog
from imp.providers.stderr_sink import StderrSink, build_stderr_sink


def _collect(tmp_path: Path, text: str, *, source: str = "claude") -> list[str]:
    got: list[str] = []
    sink = StderrSink(tmp_path / "stderr.log", on_warning=got.append, source=source)
    sink.feed(text)
    sink.close()
    return got


def test_concatenated_newlineless_warnings_dedup_to_one(tmp_path: Path) -> None:
    # The real claude form: two identical warnings, no trailing newline, no
    # separator between them. They must split at the second signature keyword
    # and then dedup to a single source-prefixed message.
    blob = ("Warning: Custom betas are only available for API key users. "
            "Ignoring provided betas."
            "Warning: Custom betas are only available for API key users. "
            "Ignoring provided betas.")
    got = _collect(tmp_path, blob)
    assert got == [
        "claude stderr: Warning: Custom betas are only available for API key "
        "users. Ignoring provided betas."
    ]


def test_warning_at_front_survives_large_trailing_output(tmp_path: Path) -> None:
    # The warning is at the front; >2 KiB of trailing output follows. The scan
    # reads the full file (not the ~2 KiB tail), so the warning is found, and
    # the trailing output on a later line is excluded from the message.
    text = "Warning: early boom\n" + ("x" * 4096)
    got = _collect(tmp_path, text)
    assert got == ["claude stderr: Warning: early boom"]


def test_distinct_signatures_emit_separately_repeats_collapse(tmp_path: Path) -> None:
    text = "Warning: alpha\nError: beta\nWarning: alpha\n"
    got = _collect(tmp_path, text)
    # Two distinct spans in first-seen order; the repeated "alpha" collapses.
    assert got == ["claude stderr: Warning: alpha", "claude stderr: Error: beta"]


def test_case_insensitive_signature_match(tmp_path: Path) -> None:
    got = _collect(tmp_path, "warning: lowercase boom\n")
    assert got == ["claude stderr: warning: lowercase boom"]


def test_lines_without_a_signature_emit_nothing(tmp_path: Path) -> None:
    got = _collect(tmp_path, "just ordinary chatter\nnothing to see here\n")
    assert got == []


def test_close_scan_runs_once_across_repeated_close(tmp_path: Path) -> None:
    got: list[str] = []
    sink = StderrSink(tmp_path / "stderr.log", on_warning=got.append, source="claude")
    sink.feed("Warning: alpha\n")
    sink.close()
    sink.close()  # idempotent: the second close must not re-emit
    assert got == ["claude stderr: Warning: alpha"]


def test_no_on_warning_hook_is_back_compat_noop(tmp_path: Path) -> None:
    log = tmp_path / "stderr.log"
    sink = StderrSink(log)  # positional path only, no hook
    sink.feed("Warning: should not crash\n")
    sink.close()  # must not raise even though a signature is present
    assert "Warning: should not crash" in log.read_text()  # capture preserved


def test_build_stderr_sink_emits_runner_warning_to_event_log(tmp_path: Path) -> None:
    elog = InMemoryEventLog()
    sink = build_stderr_sink(
        tmp_path / "stderr.log", elog=elog, aid="step:p/s", source="codex")
    sink.feed("Error: nested config drift\n")
    sink.close()
    warns = [e for e in elog.read() if e["type"] == "runner_warning"]
    assert len(warns) == 1
    assert warns[0]["agent_id"] == "step:p/s"
    assert warns[0]["payload"]["message"] == "codex stderr: Error: nested config drift"
