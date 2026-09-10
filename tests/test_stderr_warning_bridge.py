# tests/test_stderr_warning_bridge.py
from __future__ import annotations

from pathlib import Path

import pytest

from imp.engine.state import InMemoryEventLog
from imp.providers.stderr_sink import StderrSink, build_stderr_sink

_TEMP_HOME_WARNING = (
    "WARNING: proceeding, even though we could not create PATH aliases: "
    'Refusing to create helper binaries under temporary dir "/tmp" '
    '(codex_home: AbsolutePathBuf("/tmp/imp-1000/dagu48/imp-codex-123-deadbeef"))\n'
)


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


def _sink_for(tmp_path: Path, step: str, elog: InMemoryEventLog) -> StderrSink:
    return build_stderr_sink(tmp_path / step / "stderr.log", elog=elog,
                             aid=f"step:p/{step}", source="codex")


def _warnings_of(elog: InMemoryEventLog) -> list[str]:
    return [e["payload"]["message"] for e in elog.read()
            if e["type"] == "runner_warning"]


def test_codex_temp_home_warning_is_hidden_but_persisted(tmp_path: Path) -> None:
    elog = InMemoryEventLog()
    log = tmp_path / "stderr.log"
    sink = build_stderr_sink(log, elog=elog, aid="step:p/s", source="codex")
    sink.feed(_TEMP_HOME_WARNING)
    sink.close()
    assert _warnings_of(elog) == []
    assert log.read_bytes() == _TEMP_HOME_WARNING.encode("utf-8")


@pytest.mark.parametrize(
    ("source", "warning"),
    [
        (
            "codex",
            (
                "WARNING: proceeding, even though we could not create PATH aliases: "
                "permission denied\n"
            ),
        ),
        ("codex", _TEMP_HOME_WARNING.replace(
            "/tmp/imp-1000/dagu48/imp-codex-123-deadbeef", "/home/alice/.codex")),
        ("codex", _TEMP_HOME_WARNING.replace(
            "/tmp/imp-1000/dagu48/imp-codex-123-deadbeef", "/tmp/../home/alice/.codex")),
        ("codex", "Warning: unrelated Codex warning\n"),
        ("claude", _TEMP_HOME_WARNING),
    ],
    ids=[
        "generic-path-alias-permission-failure",
        "canonical-home",
        "path-traversal-home",
        "unrelated-warning",
        "claude-source",
    ],
)
def test_codex_temp_home_warning_filter_is_narrow(
    tmp_path: Path, source: str, warning: str,
) -> None:
    assert _collect(tmp_path, warning, source=source) == [
        f"{source} stderr: {' '.join(warning.split())}"
    ]


def test_same_warning_from_two_steps_emits_one_event(tmp_path: Path) -> None:
    # One sink is built per agent STEP; the dedup is run-scoped, so the second
    # step's identical provider startup warning adds no event.
    elog = InMemoryEventLog()
    for step in ("s1", "s2"):
        sink = _sink_for(tmp_path, step, elog)
        sink.feed("WARNING: proceeding, even though we could not create PATH "
                  "aliases: nope\n")
        sink.close()
    assert _warnings_of(elog) == [
        "codex stderr: WARNING: proceeding, even though we could not create PATH "
        "aliases: nope"]


def test_two_different_warnings_from_two_steps_emit_two_events(tmp_path: Path) -> None:
    elog = InMemoryEventLog()
    for step, text in (("s1", "Warning: alpha\n"), ("s2", "Warning: beta\n")):
        sink = _sink_for(tmp_path, step, elog)
        sink.feed(text)
        sink.close()
    assert _warnings_of(elog) == ["codex stderr: Warning: alpha",
                                  "codex stderr: Warning: beta"]
