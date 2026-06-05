import fcntl
import json
import os
import stat
from pathlib import Path

from imp.providers.codex_config import propagate_back


def _write(path: Path, body: dict) -> None:
    path.write_text(json.dumps(body))
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def _auth(refresh: str, *, token: str = "at") -> dict:
    return {"auth_mode": "chatgpt",
            "tokens": {"refresh_token": "rt", "access_token": token,
                       "last_refresh": refresh}}


def test_no_refresh_canonical_untouched(tmp_path: Path) -> None:
    canonical = tmp_path / "canonical.json"
    home_auth = tmp_path / "home" / "auth.json"
    home_auth.parent.mkdir(mode=0o700)
    _write(canonical, _auth("2026-06-01T00:00:00Z", token="at-orig"))
    _write(home_auth, _auth("2026-06-01T00:00:00Z", token="at-orig"))
    lock = tmp_path / ".imp-auth.lock"

    result = propagate_back(canonical_path=canonical, codex_home_auth=home_auth,
                            lock_path=lock, timeout_seconds=0.5)
    assert result == "no_refresh"
    assert json.loads(canonical.read_text())["tokens"]["access_token"] == "at-orig"


def test_refresh_writes_canonical_atomic(tmp_path: Path) -> None:
    canonical = tmp_path / "canonical.json"
    home_auth = tmp_path / "home" / "auth.json"
    home_auth.parent.mkdir(mode=0o700)
    _write(canonical, _auth("2026-06-01T00:00:00Z", token="at-old"))
    _write(home_auth, _auth("2026-06-01T05:00:00Z", token="at-new"))
    lock = tmp_path / ".imp-auth.lock"

    result = propagate_back(canonical_path=canonical, codex_home_auth=home_auth,
                            lock_path=lock, timeout_seconds=0.5)
    assert result == "written"
    assert json.loads(canonical.read_text())["tokens"]["access_token"] == "at-new"
    # No stray temp file left behind.
    temps = list(canonical.parent.glob("canonical.json.*"))
    assert temps == []


def test_monotonic_guard_blocks_stale_clobber(tmp_path: Path) -> None:
    canonical = tmp_path / "canonical.json"
    home_auth = tmp_path / "home" / "auth.json"
    home_auth.parent.mkdir(mode=0o700)
    # Canonical was advanced by a concurrent run *after* our copy-in: our
    # CODEX_HOME refreshed too (newer than copy-in), but its last_refresh is
    # OLDER than canonical's current value → guard must skip the write-back.
    _write(canonical, _auth("2026-06-01T10:00:00Z", token="at-newer-sibling"))
    _write(home_auth, _auth("2026-06-01T05:00:00Z", token="at-older-this-run"))
    lock = tmp_path / ".imp-auth.lock"

    result = propagate_back(canonical_path=canonical, codex_home_auth=home_auth,
                            lock_path=lock, timeout_seconds=0.5)
    assert result == "stale_skipped"
    # Canonical retains the newer sibling's token.
    assert json.loads(canonical.read_text())["tokens"]["access_token"] == "at-newer-sibling"


def test_lock_contention_skips_without_raising(tmp_path: Path) -> None:
    canonical = tmp_path / "canonical.json"
    home_auth = tmp_path / "home" / "auth.json"
    home_auth.parent.mkdir(mode=0o700)
    _write(canonical, _auth("2026-06-01T00:00:00Z", token="at-old"))
    _write(home_auth, _auth("2026-06-01T05:00:00Z", token="at-new"))
    lock = tmp_path / ".imp-auth.lock"

    # Hold the lock from a sibling fd for the entire call window.
    lock.touch()
    holder = os.open(str(lock), os.O_RDWR)
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        # Bounded LOCK_NB retry expires → propagate skipped, no exception.
        result = propagate_back(canonical_path=canonical, codex_home_auth=home_auth,
                                lock_path=lock, timeout_seconds=0.2)
        assert result == "lock_busy"
        assert json.loads(canonical.read_text())["tokens"]["access_token"] == "at-old"
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        os.close(holder)


def test_canonical_write_uses_os_replace_not_open_w(tmp_path: Path, monkeypatch) -> None:
    """m-1: canonical write-back must be `write-temp + os.replace()`, not a
    direct `open(canonical, 'w')`. Verifies the atomic pattern by asserting
    `os.replace` is called and no direct write hits the canonical path."""
    canonical = tmp_path / "canonical.json"
    home_auth = tmp_path / "home" / "auth.json"
    home_auth.parent.mkdir(mode=0o700)
    _write(canonical, _auth("2026-06-01T00:00:00Z", token="at-old"))
    _write(home_auth, _auth("2026-06-01T05:00:00Z", token="at-new"))
    lock = tmp_path / ".imp-auth.lock"

    calls = []
    real_replace = os.replace
    def _spy(a, b):
        calls.append((str(a), str(b)))
        real_replace(a, b)
    monkeypatch.setattr("imp.providers.codex_config.os.replace", _spy)

    propagate_back(canonical_path=canonical, codex_home_auth=home_auth,
                   lock_path=lock, timeout_seconds=0.5)
    assert len(calls) == 1
    src, dst = calls[0]
    assert dst == str(canonical)
    # Source is a sibling temp in the same directory.
    assert Path(src).parent == canonical.parent
    assert src != str(canonical)


def test_under_lock_reread_catches_concurrent_advance(tmp_path: Path, monkeypatch) -> None:
    """M3 monotonic guard under-lock re-read: when canonical advances between
    the pre-check and the lock acquisition, the locked re-read must catch it
    and return 'stale_skipped' rather than overwriting the newer canonical.

    Simulated by monkeypatching _acquire_lock_nonblocking to advance canonical
    before returning the lock fd, so the re-read under the lock sees the newer
    value.
    """
    canonical = tmp_path / "canonical.json"
    home_auth = tmp_path / "home" / "auth.json"
    home_auth.parent.mkdir(mode=0o700)
    # Home has a newer token than canonical at pre-check time.
    _write(canonical, _auth("2026-06-01T05:00:00Z", token="at-initial"))
    _write(home_auth, _auth("2026-06-01T08:00:00Z", token="at-home"))
    lock = tmp_path / ".imp-auth.lock"

    real_acquire = None

    def _advance_then_acquire(lock_path, *, timeout_seconds):
        """Simulate a concurrent run writing back an even newer token between
        the pre-check and when we acquire the lock."""
        # Write a newer token to canonical (simulating a sibling run).
        _write(canonical, _auth("2026-06-01T10:00:00Z", token="at-sibling"))
        # Now acquire the real lock.
        return real_acquire(lock_path, timeout_seconds=timeout_seconds)

    import imp.providers.codex_config as _cfg
    real_acquire = _cfg._acquire_lock_nonblocking
    monkeypatch.setattr(_cfg, "_acquire_lock_nonblocking", _advance_then_acquire)

    result = propagate_back(
        canonical_path=canonical, codex_home_auth=home_auth,
        lock_path=lock, timeout_seconds=0.5,
    )
    # Under the lock, re-read shows canonical advanced past home → stale_skipped.
    assert result == "stale_skipped"
    # Sibling's token preserved (not overwritten by our stale home token).
    assert json.loads(canonical.read_text())["tokens"]["access_token"] == "at-sibling"
