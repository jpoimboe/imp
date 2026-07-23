from __future__ import annotations

import hashlib
import os
import socket
import tempfile
from pathlib import Path

from imp.remote.daemon.discover import socket_is_live, socket_path


def test_socket_path_is_tmp_rc_digest(tmp_path: Path) -> None:
    digest = hashlib.sha256(os.fsencode(tmp_path.resolve())).hexdigest()[:16]
    assert socket_path(tmp_path) == (
        Path(tempfile.gettempdir()) / f"imp-{os.getuid()}" / f"rc-{digest}.sock")


def test_socket_path_leaf_length_is_independent_of_depth() -> None:
    shallow = socket_path(Path("/a"))
    deep = socket_path(
        Path("/a/bbbbbbbbbb/cccccccccc/dddddddddd/eeeeeeeeee/ffffffffff"))
    assert len(shallow.name) == len(deep.name)


def test_socket_path_is_stable_for_the_same_root(tmp_path: Path) -> None:
    assert socket_path(tmp_path) == socket_path(tmp_path)


def test_socket_path_differs_for_different_roots(tmp_path: Path) -> None:
    assert socket_path(tmp_path / "a") != socket_path(tmp_path / "b")


def test_socket_is_live_false_when_absent(tmp_path: Path) -> None:
    assert socket_is_live(tmp_path / "absent.sock") is False


def test_socket_is_live_false_for_a_stray_socket_with_no_listener(tmp_path: Path) -> None:
    # A bound-then-closed socket leaves the file on disk but nothing listening:
    # connecting is refused, so it must NOT count as live (spec: a stray socket
    # never pulls in an imp).
    path = tmp_path / "rc.sock"
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(path))
    s.close()
    assert socket_is_live(path) is False


def test_socket_is_live_true_when_a_listener_accepts(tmp_path: Path) -> None:
    path = tmp_path / "rc.sock"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(path))
    srv.listen()
    try:
        assert socket_is_live(path) is True
    finally:
        srv.close()
