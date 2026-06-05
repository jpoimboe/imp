import fcntl
import json
import os
import stat
from pathlib import Path

from imp.providers.codex_config import teardown_codex_home


def _seed(home: Path, *, with_auth: bool = True) -> None:
    home.mkdir(mode=0o700, parents=True)
    if with_auth:
        body = {"auth_mode": "chatgpt",
                "tokens": {"refresh_token": "rt", "access_token": "at",
                           "last_refresh": "2026-06-01T00:00:00Z"}}
        (home / "auth.json").write_text(json.dumps(body))
        (home / "auth.json").chmod(stat.S_IRUSR | stat.S_IWUSR)
    (home / "config.toml").write_text("[mcp_servers.imp]\n")


def test_teardown_removes_directory_on_apikey_mode(tmp_path: Path) -> None:
    home = tmp_path / "codex-home"
    _seed(home)
    teardown_codex_home(
        codex_home=home, mode="apikey", canonical_path=tmp_path / "canon",
        lock_path=tmp_path / ".imp-auth.lock",
    )
    assert not home.exists()


def test_teardown_chatgpt_runs_propagate_then_shred_then_unlink(tmp_path: Path) -> None:
    canonical = tmp_path / "canonical.json"
    canonical.write_text(json.dumps({
        "auth_mode": "chatgpt",
        "tokens": {"refresh_token": "rt", "access_token": "at-old",
                   "last_refresh": "2026-06-01T00:00:00Z"}}))
    home = tmp_path / "codex-home"
    home.mkdir(mode=0o700)
    # Simulate a refresh: CODEX_HOME's last_refresh is newer.
    body = {"auth_mode": "chatgpt",
            "tokens": {"refresh_token": "rt", "access_token": "at-new",
                       "last_refresh": "2026-06-01T05:00:00Z"}}
    (home / "auth.json").write_text(json.dumps(body))
    (home / "auth.json").chmod(stat.S_IRUSR | stat.S_IWUSR)

    teardown_codex_home(
        codex_home=home, mode="chatgpt", canonical_path=canonical,
        lock_path=tmp_path / ".imp-auth.lock",
    )
    # Canonical absorbed the rotated token (propagate-back ran first).
    assert json.loads(canonical.read_text())["tokens"]["access_token"] == "at-new"
    # Then dir was removed (shred+unlink ran).
    assert not home.exists()


def test_teardown_shreds_even_when_propagate_skipped_lock_busy(tmp_path: Path) -> None:
    """n-5: shred must run unconditionally, even when propagate-back is
    skipped (LOCK_NB retry expired)."""
    canonical = tmp_path / "canonical.json"
    canonical.write_text(json.dumps({
        "auth_mode": "chatgpt",
        "tokens": {"refresh_token": "rt", "access_token": "at-old",
                   "last_refresh": "2026-06-01T00:00:00Z"}}))
    home = tmp_path / "codex-home"
    _seed(home)
    # Hold the lock so propagate-back returns "lock_busy".
    lock = tmp_path / ".imp-auth.lock"
    lock.touch()
    holder = os.open(str(lock), os.O_RDWR)
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        teardown_codex_home(
            codex_home=home, mode="chatgpt", canonical_path=canonical,
            lock_path=lock, propagate_timeout_seconds=0.05,
        )
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        os.close(holder)
    # Canonical was NOT touched (lock_busy).
    assert json.loads(canonical.read_text())["tokens"]["access_token"] == "at-old"
    # But the CODEX_HOME credential is gone (shred + unlink ran anyway).
    assert not home.exists()
