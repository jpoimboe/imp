# tests/test_remote_channel_stderr.py
"""The host promotes a driver's severity-tagged stderr line to that log level.

A driver's stderr is normally DEBUG (default-silent, keeps the --tui alt-screen
clean). A driver may opt a structural failure into visibility by prefixing the
line with `WARNING:` / `ERROR:`; the host maps that to the logging level so it
reaches Python's lastResort handler (WARNING+) by default. Untagged lines stay
DEBUG (backward compatible)."""
from __future__ import annotations

import logging

from imp.cli import remote_channel


def test_untagged_stderr_line_stays_debug() -> None:
    assert remote_channel._classify_stderr_line("send failed: boom") == (
        logging.DEBUG, "send failed: boom")


def test_warning_tag_is_promoted_and_stripped() -> None:
    assert remote_channel._classify_stderr_line(
        "WARNING: imp-telegram: token rejected") == (
        logging.WARNING, "imp-telegram: token rejected")


def test_error_tag_is_promoted_and_stripped() -> None:
    assert remote_channel._classify_stderr_line("ERROR: boom") == (
        logging.ERROR, "boom")


def test_tag_must_be_at_line_start() -> None:
    # A level word mid-line is not a marker — only a leading tag counts.
    assert remote_channel._classify_stderr_line(
        "imp-telegram: WARNING ahead") == (
        logging.DEBUG, "imp-telegram: WARNING ahead")
