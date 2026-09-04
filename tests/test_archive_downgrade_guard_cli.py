"""imp list / imp show against an archive newer than this install: one imp: line, exit 2."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from imp.cli.main import main
from imp.engine import archive
from imp.engine.errors import ArchiveMismatch
from tests.conftest import _archive_with_event, _seed_control

ARCH = "arqu99"

_EVENTS = json.dumps({
    "id": "e0", "timestamp": "2026-06-01T00:00:01+00:00", "type": "user_message",
    "payload": {"role": "user", "subtype": "intent",
                "content": [{"type": "text", "text": "flywheel"}]}}) + "\n"


def _seed_newer_archive(control: Path) -> int:
    """One archived quest, then the blob floor stamped one ahead of this install."""
    _archive_with_event(control, ARCH, "Archived Thing", _EVENTS)
    ahead = archive._DATA_VERSION + 1
    archive.data_version_path(control).write_text(str(ahead))
    return ahead


def _expected_stderr_line(ahead: int) -> str:
    """The error's own sentence, prefixed by _emit_cli_error's `imp: ` — nothing else.
    The exact wording is pinned in tests/test_archive_mismatch_error.py."""
    exc = ArchiveMismatch(floor="data", stored=ahead, known=archive._DATA_VERSION)
    return f"imp: {exc}"


def _arrange(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> int:
    import imp.cli.main as m

    _seed_control(tmp_path)
    ahead = _seed_newer_archive(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setenv("NO_COLOR", "1")
    return ahead


def test_list_emits_one_error_line_and_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ahead = _arrange(tmp_path, monkeypatch)

    rc = main(["list"])

    captured = capsys.readouterr()
    assert rc == 2
    assert captured.err.splitlines() == [_expected_stderr_line(ahead)]
    assert captured.out.strip() == ""      # no rows rendered before the refusal


def test_show_surfaces_the_same_line_and_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """id resolution reaches the archive through _iter_quest_ids: no intermediate
    handler turns the refusal into a rehydrate fallback, an empty result, or
    'no quest matches'."""
    ahead = _arrange(tmp_path, monkeypatch)

    rc = main(["show", ARCH])

    captured = capsys.readouterr()
    assert rc == 2
    assert captured.err.splitlines() == [_expected_stderr_line(ahead)]
    assert captured.out.strip() == ""
