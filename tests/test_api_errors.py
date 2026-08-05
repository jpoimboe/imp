"""The embedding API's exception hierarchy: one base every consumer can catch."""

from __future__ import annotations

from pathlib import Path

import pytest

from imp.api_errors import (
    DaemonUnreachable,
    ImpError,
    InvalidVar,
    NotAtGate,
    QuestNotFound,
)
from imp.remote.session.attach import DaemonStartupError


@pytest.mark.parametrize(
    "cls", [QuestNotFound, DaemonUnreachable, NotAtGate, InvalidVar])
def test_every_public_error_derives_from_imp_error(cls: type[Exception]) -> None:
    assert issubclass(cls, ImpError)


def test_imp_error_is_not_broader_than_exception() -> None:
    # ImpError must not shadow a builtin family a consumer catches separately.
    assert ImpError.__bases__ == (Exception,)


def test_daemon_startup_error_is_catchable_as_daemon_unreachable() -> None:
    err = DaemonStartupError("feat12", 7, Path("/tmp/daemon.stderr"), "boom")
    assert isinstance(err, DaemonUnreachable)
    assert isinstance(err, ImpError)


def test_daemon_startup_error_keeps_its_structured_fields() -> None:
    # Re-parenting must not disturb the fields `_emit_startup_failed` renders.
    err = DaemonStartupError("feat12", 7, Path("/tmp/daemon.stderr"), "boom")
    assert (err.quest_id, err.exit_code, err.stderr_tail) == ("feat12", 7, "boom")
    assert err.stderr_path == Path("/tmp/daemon.stderr")
    assert "before it came up" in str(err)
