"""Cover the basetemp derivation + teardown reaping in conftest."""

from __future__ import annotations

import os
import re
import socket
import tempfile
from pathlib import Path
from types import SimpleNamespace

import tests.conftest as conftest

_SUN_PATH_MAX = 107   # AF_UNIX sockaddr_un.sun_path, the ceiling tmp_path must respect


def _config(basetemp=None):
    return SimpleNamespace(option=SimpleNamespace(basetemp=basetemp))


def test_configure_roots_basetemp_directly_under_gettempdir(monkeypatch):
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    monkeypatch.setattr(conftest, "_OWNED_BASETEMP", None)
    config = _config()
    conftest.pytest_configure(config)
    basetemp = Path(config.option.basetemp)
    assert basetemp.parent == Path(tempfile.gettempdir())
    assert re.fullmatch(r"ip[0-9a-f]{6}", basetemp.name)
    assert config.option.basetemp == conftest._OWNED_BASETEMP


def test_configure_basetemp_follows_tmpdir(monkeypatch):
    # Inside the sandbox TMPDIR is the per-quest scratch dir and host /tmp is
    # read-only, so the basetemp must root under TMPDIR — and still leave ample
    # room under the sun_path limit for pytest's own tmp_path suffixes.
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    monkeypatch.setattr(conftest, "_OWNED_BASETEMP", None)
    scratch = f"/tmp/imp-{os.getuid()}/2026-06-15-interrupted"
    monkeypatch.setenv("TMPDIR", scratch)
    monkeypatch.setattr(tempfile, "tempdir", scratch)
    config = _config()
    conftest.pytest_configure(config)
    basetemp = Path(config.option.basetemp)
    assert basetemp.parent == Path(scratch)
    assert re.fullmatch(r"ip[0-9a-f]{6}", basetemp.name)
    # pytest's worst case under this basetemp: an xdist worker's own basetemp
    # segment, then a 30-char test-name dir with its numbered suffix, holding a
    # socket bound directly inside it.
    worst = basetemp / "popen-gw15" / ("t" * 30 + "0") / "rc.sock"
    assert len(str(worst)) < _SUN_PATH_MAX


def test_a_socket_binds_directly_under_tmp_path(tmp_path):
    # The contract the basetemp derivation exists to protect: tests hand-bind
    # AF_UNIX sockets directly under tmp_path, so the path must fit sun_path.
    sock = tmp_path / "rc.sock"
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.bind(str(sock))
    finally:
        s.close()
    assert sock.is_socket()
    assert len(str(sock)) < _SUN_PATH_MAX


def test_configure_skips_user_supplied_basetemp(monkeypatch):
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    monkeypatch.setattr(conftest, "_OWNED_BASETEMP", None)
    config = _config(basetemp="/custom/bt")
    conftest.pytest_configure(config)
    assert config.option.basetemp == "/custom/bt"
    assert conftest._OWNED_BASETEMP is None


def test_configure_skips_xdist_worker(monkeypatch):
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")
    monkeypatch.setattr(conftest, "_OWNED_BASETEMP", None)
    config = _config()
    conftest.pytest_configure(config)
    assert config.option.basetemp is None
    assert conftest._OWNED_BASETEMP is None


def test_unconfigure_reaps_owned_basetemp(tmp_path, monkeypatch):
    owned = tmp_path / "ipabc123"
    owned.mkdir()
    live_home = conftest._OWNED_IMP_HOME
    monkeypatch.setattr(conftest, "_OWNED_BASETEMP", str(owned))
    monkeypatch.setattr(conftest, "_OWNED_IMP_HOME", None)
    conftest.pytest_unconfigure(_config())
    assert not owned.exists()
    assert Path(live_home).is_dir()


def test_unconfigure_noop_when_not_owned(tmp_path, monkeypatch):
    survivor = tmp_path / "user-basetemp"
    survivor.mkdir()
    live_home = conftest._OWNED_IMP_HOME
    monkeypatch.setattr(conftest, "_OWNED_BASETEMP", None)
    monkeypatch.setattr(conftest, "_OWNED_IMP_HOME", None)
    conftest.pytest_unconfigure(_config())
    assert survivor.exists()
    assert Path(live_home).is_dir()


def test_configure_pins_imp_home_off_the_real_home(monkeypatch):
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    monkeypatch.setattr(conftest, "_OWNED_BASETEMP", None)
    monkeypatch.setattr(conftest, "_OWNED_IMP_HOME", None)
    conftest.pytest_configure(_config(basetemp="/custom/bt"))
    pinned = Path(conftest._OWNED_IMP_HOME)
    try:
        assert os.environ["IMP_HOME"] == str(pinned)
        assert pinned.is_dir()
        assert pinned != Path.home() / ".imp"
    finally:
        conftest.pytest_unconfigure(_config())
    assert not pinned.exists()


def test_configure_leaves_an_existing_imp_home_pin_alone(monkeypatch):
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    monkeypatch.setattr(conftest, "_OWNED_BASETEMP", None)
    monkeypatch.setattr(conftest, "_OWNED_IMP_HOME", "/sentinel/imphome")
    before = os.environ["IMP_HOME"]
    conftest.pytest_configure(_config(basetemp="/custom/bt"))
    assert conftest._OWNED_IMP_HOME == "/sentinel/imphome"
    assert os.environ["IMP_HOME"] == before
