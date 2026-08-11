"""Cover the fixed-root basetemp derivation + teardown reaping in conftest."""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from types import SimpleNamespace

import tests.conftest as conftest


def _config(basetemp=None):
    return SimpleNamespace(option=SimpleNamespace(basetemp=basetemp))


def test_configure_roots_basetemp_at_short_fixed_tmp(monkeypatch):
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    monkeypatch.setattr(conftest, "_OWNED_BASETEMP", None)
    config = _config()
    conftest.pytest_configure(config)
    assert re.fullmatch(r"/tmp/ip[0-9a-f]{6}", config.option.basetemp)
    assert config.option.basetemp == conftest._OWNED_BASETEMP


def test_configure_basetemp_independent_of_long_tmpdir(monkeypatch):
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    monkeypatch.setattr(conftest, "_OWNED_BASETEMP", None)
    long_tmp = "/tmp/imp-1000/2026-06-15-interrupted"
    monkeypatch.setenv("TMPDIR", long_tmp)
    monkeypatch.setattr(tempfile, "tempdir", long_tmp)
    config = _config()
    conftest.pytest_configure(config)
    assert config.option.basetemp.startswith("/tmp/ip")
    assert not config.option.basetemp.startswith(long_tmp)


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
    monkeypatch.setattr(conftest, "_OWNED_IMP_HOME", "/tmp/imphome-sentinel")
    before = os.environ["IMP_HOME"]
    conftest.pytest_configure(_config(basetemp="/custom/bt"))
    assert conftest._OWNED_IMP_HOME == "/tmp/imphome-sentinel"
    assert os.environ["IMP_HOME"] == before
