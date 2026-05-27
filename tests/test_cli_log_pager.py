"""Tests for the imp log pager (src/imp/cli/main.py::_page_or_print).

Each test patches `subprocess.Popen` at the import site
(`imp.cli.main.subprocess.Popen`) so no real pager is spawned.

The helper takes `lines: Iterable[str]` (no trailing newlines) and is
expected to write one line per iteration, appending "\n" itself — both
in the pager-spawn path (via `proc.stdin.write`) and in the fallback
print path (via `print()` which adds its own newline).
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from imp.cli.main import main


def test_no_pager_flag_parses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """`imp log --no-pager` parses without error and exits 0 against a
    minimal Change. This locks in the argparse wiring before _page_or_print
    exists."""
    cdir = tmp_path / ".imp" / "changes" / "1"
    cdir.mkdir(parents=True)
    (cdir / "change.json").write_text(json.dumps({"id": 1, "title": "smoke"}))
    (cdir / "events.jsonl").write_text(
        json.dumps({
            "timestamp": "2026-05-27T10:00:00+00:00",
            "type": "change_created",
            "payload": {"change_id": 1, "title": "smoke"},
        }) + "\n",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("NO_COLOR", raising=False)

    rc = main(["log", "--no-pager", "1"])
    assert rc == 0
    assert "change_created" in capsys.readouterr().out
