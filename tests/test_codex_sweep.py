import os
import time
from pathlib import Path

from imp.providers.codex_config import sweep_stale_codex_homes


def _mk(home_root: Path, name: str, *, age_seconds: float = 0.0) -> Path:
    d = home_root / name
    d.mkdir(mode=0o700)
    if age_seconds > 0:
        past = time.time() - age_seconds
        os.utime(d, (past, past))
    return d


def test_sweep_skips_live_owner_pid(tmp_path: Path) -> None:
    """L4: a dir owned by a still-alive pid must NOT be removed, even if its
    age exceeds the bound."""
    my_pid = os.getpid()
    live = _mk(tmp_path, f"imp-codex-{my_pid}-abcd1234", age_seconds=24 * 3600)
    sweep_stale_codex_homes(tmp_root=tmp_path, max_age_seconds=60)
    assert live.exists()


def test_sweep_removes_dead_owner_pid_over_age(tmp_path: Path) -> None:
    """Dir whose encoded pid is dead AND whose age > bound is removed."""
    dead = _mk(tmp_path, "imp-codex-999999-cafef00d", age_seconds=24 * 3600)
    sweep_stale_codex_homes(tmp_root=tmp_path, max_age_seconds=60)
    assert not dead.exists()


def test_sweep_keeps_dead_owner_pid_under_age(tmp_path: Path) -> None:
    """Dir is dead-owner but recent → keep (within bound)."""
    recent = _mk(tmp_path, "imp-codex-999999-deadbeef", age_seconds=0.0)
    sweep_stale_codex_homes(tmp_root=tmp_path, max_age_seconds=3600)
    assert recent.exists()


def test_sweep_ignores_non_imp_dirs(tmp_path: Path) -> None:
    other = _mk(tmp_path, "some-unrelated-dir", age_seconds=24 * 3600)
    sweep_stale_codex_homes(tmp_root=tmp_path, max_age_seconds=60)
    assert other.exists()
