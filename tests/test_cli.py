import json
import re
from pathlib import Path

import pytest

from imp.cli.main import _last_event_timestamp, main
from tests.conftest import write_agent_md

FIXTURES = Path(__file__).parent / "fixtures"


def _seed_default_workflow(project: Path) -> None:
    src = project / ".imp" / "workflows" / "default"
    (src / "agents").mkdir(parents=True)
    write_agent_md(
        src / "agents" / "context-loader.md",
        frontmatter=(
            "name: context-loader\ntools: [complete_step]\n"
            "output:\n  status: {type: enum, values: [Success, Error]}\n"
            "  notes: {type: string, required: false}"
        ),
    )
    (src / "workflow.yaml").write_text(
        "name: t\ndescription: \"\"\nphases: [p]\n"
        "p:\n  steps: [{name: context-loader, file: agents/context-loader.md}]\n"
    )
    (project / ".imp" / "config.yaml").write_text(
        "roles:\n  default: {prefer: [{provider: anthropic, model: x}]}\n"
    )


def test_new_completes_change(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture,
) -> None:
    _seed_default_workflow(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(
        "IMP_STUB_CANNED", str(FIXTURES / "canned" / "stub_outputs.yaml")
    )
    code = main(["new", "my-change"])
    assert code == 0
    out = capsys.readouterr().out
    assert "completed" in out.lower()
    state_file = tmp_path / ".imp" / "changes" / "1" / "state" / "state.json"
    assert state_file.exists()
    assert json.loads(state_file.read_text())["status"] == "completed"


def test_new_requires_no_other_running(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _seed_default_workflow(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(
        "IMP_STUB_CANNED", str(FIXTURES / "canned" / "stub_outputs.yaml")
    )
    assert main(["new", "a"]) == 0
    assert main(["new", "b"]) == 0


def test_resume_paused_change(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    _seed_default_workflow(tmp_path)
    monkeypatch.chdir(tmp_path)
    # Override workflow.yaml to a paused-after-first-step pattern.
    (tmp_path / ".imp" / "workflows" / "default" / "workflow.yaml").write_text(
        "name: t\ndescription: \"\"\nphases: [p, q]\n"
        "p:\n  steps: [{name: context-loader, file: agents/context-loader.md}]\n"
        "  gates:\n"
        "    - type: script\n"
        "      command: \"false\"\n"
        "      decide:\n"
        "        - {when: {exit: 0}, action: {kind: continue}}\n"
        "        - {else: true, action: {kind: pause}}\n"
        "q:\n  steps: [{name: context-loader, file: agents/context-loader.md}]\n"
    )
    monkeypatch.setenv(
        "IMP_STUB_CANNED", str(FIXTURES / "canned" / "stub_outputs.yaml")
    )
    main(["new", "x"])  # will pause at phase p's gate
    capsys.readouterr()
    # Edit workflow so resume succeeds: replace `false` with `true`.
    wf_path = tmp_path / ".imp" / "changes" / "1" / "workflow" / "workflow.yaml"
    wf_path.write_text(wf_path.read_text().replace("false", "true"))
    code = main(["resume", "1"])
    assert code == 0
    out = capsys.readouterr().out
    assert "replaying" in out.lower()


def test_resume_unknown_id_returns_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    _seed_default_workflow(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IMP_STUB_CANNED",
                       str(FIXTURES / "canned" / "stub_outputs.yaml"))
    code = main(["resume", "999"])
    assert code == 2
    err = capsys.readouterr().err
    assert "no such Change" in err


def test_log_unknown_id_returns_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    _seed_default_workflow(tmp_path)
    monkeypatch.chdir(tmp_path)
    code = main(["log", "999"])
    assert code == 2
    err = capsys.readouterr().err
    assert "no such Change" in err


def test_status_with_stray_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    _seed_default_workflow(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IMP_STUB_CANNED",
                       str(FIXTURES / "canned" / "stub_outputs.yaml"))
    main(["new", "a"])
    capsys.readouterr()
    # Stray non-numeric file in .imp/changes/ shouldn't crash status.
    (tmp_path / ".imp" / "changes" / "stray.txt").write_text("hi")
    (tmp_path / ".imp" / "changes" / "not-a-number").mkdir()
    code = main(["status"])
    assert code == 0
    out = capsys.readouterr().out
    assert "1" in out  # the real change shows up


def test_new_reports_loaderror_cleanly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """Missing config.yaml should produce a friendly error, not a traceback."""
    # Seed workflow but NOT config.yaml
    src = tmp_path / ".imp" / "workflows" / "default"
    (src / "agents").mkdir(parents=True)
    write_agent_md(
        src / "agents" / "context-loader.md",
        frontmatter=(
            "name: context-loader\ntools: [complete_step]\n"
            "output:\n  status: {type: enum, values: [Success, Error]}\n"
            "  notes: {type: string, required: false}"
        ),
    )
    (src / "workflow.yaml").write_text(
        "name: t\ndescription: \"\"\nphases: [p]\n"
        "p:\n  steps: [{name: context-loader, file: agents/context-loader.md}]\n"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IMP_STUB_CANNED",
                       str(FIXTURES / "canned" / "stub_outputs.yaml"))
    code = main(["new", "x"])
    assert code == 2
    err = capsys.readouterr().err
    assert "imp:" in err
    assert "config.yaml" in err


def test_log_no_id_falls_back_to_latest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """`imp log` with no ID should default to the most recent Change."""
    _seed_default_workflow(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IMP_STUB_CANNED",
                       str(FIXTURES / "canned" / "stub_outputs.yaml"))
    main(["new", "first"])
    main(["new", "second"])
    capsys.readouterr()
    code = main(["log"])
    assert code == 0
    out = capsys.readouterr().out
    # The most recent change is #2, so events for it (change_created) should appear.
    # Each change has its own log; verify we got events.
    assert "phase_started" in out or "change_created" in out


def test_log_no_id_no_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """`imp log` with no ID and no Changes yet should error cleanly."""
    (tmp_path / ".imp").mkdir()
    monkeypatch.chdir(tmp_path)
    code = main(["log"])
    assert code == 2
    err = capsys.readouterr().err
    assert "no Changes" in err


# ---------------------------------------------------------------------------
# _last_event_timestamp unit tests
# ---------------------------------------------------------------------------

def test_last_event_timestamp_with_events(tmp_path: Path) -> None:
    """A valid events.jsonl yields a formatted local-time timestamp."""
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(
        '{"timestamp": "2024-03-15T10:30:00+00:00", "type": "change_created", "payload": {}}\n'
        '{"timestamp": "2024-03-15T11:45:00+00:00", "type": "step_started", "payload": {}}\n'
    )
    result = _last_event_timestamp(events_path)
    # Result must be a 19-char YYYY-MM-DD HH:MM:SS string (exact time depends on local tz).
    assert len(result) == 19
    assert result[4] == "-" and result[7] == "-" and result[10] == " "
    assert result[13] == ":" and result[16] == ":"
    # Must not be the sentinel value.
    assert result != "-"


def test_last_event_timestamp_missing_file(tmp_path: Path) -> None:
    """Missing events.jsonl returns '-'."""
    result = _last_event_timestamp(tmp_path / "events.jsonl")
    assert result == "-"


def test_last_event_timestamp_empty_file(tmp_path: Path) -> None:
    """Empty events.jsonl returns '-'."""
    events_path = tmp_path / "events.jsonl"
    events_path.write_text("")
    assert _last_event_timestamp(events_path) == "-"


def test_last_event_timestamp_malformed_last_line(tmp_path: Path) -> None:
    """Malformed trailing line returns '-' without raising."""
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(
        '{"timestamp": "2024-03-15T10:00:00+00:00", "type": "ok", "payload": {}}\n'
        "not valid json\n"
    )
    assert _last_event_timestamp(events_path) == "-"


def test_status_header_contains_last_activity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """Header row of `imp status` must include LAST_ACTIVITY column."""
    _seed_default_workflow(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IMP_STUB_CANNED",
                       str(FIXTURES / "canned" / "stub_outputs.yaml"))
    main(["new", "my-change"])
    capsys.readouterr()
    code = main(["status"])
    assert code == 0
    out = capsys.readouterr().out
    header = out.splitlines()[0]
    assert "LAST_ACTIVITY" in header
    # Column order: STEP must come before LAST_ACTIVITY, which must precede TITLE.
    assert header.index("STEP") < header.index("LAST_ACTIVITY") < header.index("TITLE")


def test_status_data_row_shows_timestamp(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """Data rows show a real timestamp after running a Change."""
    _seed_default_workflow(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IMP_STUB_CANNED",
                       str(FIXTURES / "canned" / "stub_outputs.yaml"))
    main(["new", "ts-test"])
    capsys.readouterr()
    code = main(["status"])
    assert code == 0
    out = capsys.readouterr().out
    data_row = out.splitlines()[1]
    # A timestamp looks like YYYY-MM-DD HH:MM:SS — check at least the date part.
    assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", data_row)


def test_status_no_events_shows_dash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """A Change whose events.jsonl is missing renders '-' in LAST_ACTIVITY."""
    _seed_default_workflow(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IMP_STUB_CANNED",
                       str(FIXTURES / "canned" / "stub_outputs.yaml"))
    main(["new", "no-events"])
    capsys.readouterr()
    # Remove events.jsonl to simulate missing file.
    events_path = tmp_path / ".imp" / "changes" / "1" / "events.jsonl"
    events_path.unlink(missing_ok=True)
    code = main(["status"])
    assert code == 0
    out = capsys.readouterr().out
    data_row = out.splitlines()[1]
    # The LAST_ACTIVITY column should be "-".
    assert " - " in data_row or data_row.strip().endswith("-")


def test_default_runner_is_deepagent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("IMP_STUB_CANNED", raising=False)
    from imp.agents.deepagent_runner import DeepAgentRunner
    from imp.cli.main import _agent_runner

    runner = _agent_runner()
    assert isinstance(runner, DeepAgentRunner)


def test_stub_runner_honored_when_env_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    canned = tmp_path / "canned.yaml"
    canned.write_text(
        "stub:\n  events:\n  - {type: agent_output, payload: {status: Success}}\n"
    )
    monkeypatch.setenv("IMP_STUB_CANNED", str(canned))
    from imp.agents.stub_runner import StubAgentRunner
    from imp.cli.main import _agent_runner

    runner = _agent_runner()
    assert isinstance(runner, StubAgentRunner)
