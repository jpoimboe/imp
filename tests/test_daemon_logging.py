from __future__ import annotations

import io
import logging
import sys
from pathlib import Path

import pytest

from imp.remote.daemon import runner


def _log_text(control_root: Path) -> str:
    return (control_root / ".imp" / "remote-control.log").read_text()


def test_install_writes_info_line_with_format(tmp_path: Path) -> None:
    (tmp_path / ".imp").mkdir()
    handlers = runner._install_file_logging(tmp_path, {})
    try:
        logging.getLogger("imp.daemon").info("hello-from-daemon")
    finally:
        runner._remove_file_logging(handlers)
    text = _log_text(tmp_path)
    assert "hello-from-daemon" in text
    assert "INFO imp.daemon:" in text


def test_imp_debug_enables_debug_lines(tmp_path: Path) -> None:
    (tmp_path / ".imp").mkdir()
    handlers = runner._install_file_logging(tmp_path, {"IMP_DEBUG": "1"})
    try:
        logging.getLogger("imp.remote").debug("debug-marker")
    finally:
        runner._remove_file_logging(handlers)
    assert "debug-marker" in _log_text(tmp_path)


def test_debug_lines_absent_without_imp_debug(tmp_path: Path) -> None:
    (tmp_path / ".imp").mkdir()
    handlers = runner._install_file_logging(tmp_path, {})
    try:
        logging.getLogger("imp.remote").debug("debug-marker")
        logging.getLogger("imp.remote").info("info-marker")
    finally:
        runner._remove_file_logging(handlers)
    text = _log_text(tmp_path)
    assert "debug-marker" not in text
    assert "info-marker" in text


def test_warning_reaches_stderr_info_does_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / ".imp").mkdir()
    err = io.StringIO()
    monkeypatch.setattr(sys, "stderr", err)
    handlers = runner._install_file_logging(tmp_path, {})
    try:
        logging.getLogger("imp.daemon").warning("stderr-warning-marker")
        logging.getLogger("imp.daemon").info("stderr-info-marker")
    finally:
        runner._remove_file_logging(handlers)
    captured = err.getvalue()
    assert "stderr-warning-marker" in captured
    assert "stderr-info-marker" not in captured


def test_unwritable_imp_falls_back_with_one_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    (tmp_path / ".imp").write_text("not a dir")
    with caplog.at_level(logging.WARNING, logger="imp.daemon"):
        handlers = runner._install_file_logging(tmp_path, {})
    assert handlers == []
    assert "file logging disabled" in caplog.text
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
