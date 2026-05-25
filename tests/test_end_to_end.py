import json
import shutil
from pathlib import Path

import pytest

from imp.cli.main import main

FIXTURES = Path(__file__).parent / "fixtures"


def test_full_lifecycle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture,
) -> None:
    # Seed the project
    shutil.copytree(
        FIXTURES / "workflows" / "integration",
        tmp_path / ".imp" / "workflows" / "default",
    )
    (tmp_path / ".imp" / "config.yaml").write_text(
        "roles:\n  default: {prefer: [{provider: anthropic, model: x}]}\n"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(
        "IMP_STUB_CANNED", str(FIXTURES / "canned" / "stub_outputs.yaml")
    )

    # imp new → should pause at `write` phase gate (command: false).
    rc = main(["new", "integration-test"])
    assert rc != 0  # exit non-zero on pause
    state = json.loads(
        (tmp_path / ".imp" / "changes" / "1" / "state" / "state.json").read_text()
    )
    assert state["status"] == "paused"
    assert state["phase"] == "write"

    # imp status shows it.
    capsys.readouterr()
    main(["status"])
    out = capsys.readouterr().out
    assert "paused" in out

    # imp log dumps events.
    main(["log", "1"])
    log_out = capsys.readouterr().out
    assert "phase_started" in log_out
    assert "workflow_paused" in log_out

    # Fix the gate in the snapshot and resume.
    snap_yaml = (
        tmp_path / ".imp" / "changes" / "1" / "workflow" / "workflow.yaml"
    )
    snap_yaml.write_text(snap_yaml.read_text().replace("\"false\"", "\"true\""))

    rc = main(["resume", "1"])
    assert rc == 0
    state = json.loads(
        (tmp_path / ".imp" / "changes" / "1" / "state" / "state.json").read_text()
    )
    assert state["status"] == "completed"

    # Final log includes the verify phase.
    main(["log", "1"])
    final_log = capsys.readouterr().out
    assert "verify" in final_log
