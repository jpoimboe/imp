"""Per-run CODEX_HOME provisioning + auth handling (spec §3, design "run() flow"
step 3, "Lifecycle / teardown", and "Auth copy-in + propagate-back").

Implements only what the SDK can't express: the MCP server URL + bearer-token
env-var name, the [features] toggles, and the auth.json seed. Everything else
(model, sandbox_mode, approval_policy, web_search_enabled, network_access_enabled)
goes through SDK options to keep config precedence single-channel.

Filesystem invariants:
  - CODEX_HOME dir is mode 0700 under TMPDIR, with the owner pid encoded in
    the dir name so the startup sweep can skip live-owner dirs (L4).
  - auth.json and config.toml are mode 0600.
  - canonical write-back is atomic (write-temp + os.replace) so a lockless
    copy-in cannot read a torn file (m-1).
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import json
import logging
import os
import re as _re
import secrets
import shutil
import stat
import subprocess
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

_logger = logging.getLogger(__name__)

# ── render_config_toml ──────────────────────────────────────────────────────

def render_config_toml(
    *, host: str, port: int, bearer_env_var: str, web_search_enabled: bool,
) -> str:
    """Emit the `config.toml` text for a per-run CODEX_HOME.

    Carries the MCP server entry, the [features] toggles, and the top-level
    `web_search` control. `web_search` lives **here**, not in SDK options,
    because codex >=0.137 deprecated the SDK-emitted `features.web_search_request`
    key and ignores it for disabling (web search is on by default) — so the
    SDK-option channel can no longer turn it off. The top-level `web_search` key
    is the only mechanism that actually disables model-hosted search, which is a
    security boundary (model-hosted search bypasses the `networkAccessEnabled=
    False` sandbox lock). It is set to "disabled" unless the running agent
    declared the `web_search` tool. Verified live 2026-06-05 on codex 0.137.0.

    A top-level key must precede every `[table]` in TOML, so `web_search` is
    emitted first.
    """
    web_search = "live" if web_search_enabled else "disabled"
    return (
        f'web_search = "{web_search}"\n'
        "\n"
        "[mcp_servers.imp]\n"
        f'url = "http://{host}:{port}/mcp"\n'
        f'bearer_token_env_var = "{bearer_env_var}"\n'
        "\n"
        "[features]\n"
        "shell_tool = false\n"
        "plugins = false\n"
        "image_generation = false\n"
    )


# ── provision_codex_home ────────────────────────────────────────────────────

_DIR_PREFIX = "imp-codex-"


def provision_codex_home(
    *, tmp_root: Path, host: str, port: int, bearer_env_var: str,
    web_search_enabled: bool,
) -> Path:
    """Create a per-run CODEX_HOME under `tmp_root` mode 0700, write its
    config.toml mode 0600, and return the dir path.

    Dir name shape: `imp-codex-<pid>-<8-hex>` — the pid lets the startup sweep
    (L4) skip live-owner dirs without depending on the age bound; the entropy
    suffix makes the name unique across concurrent runs from one pid.

    `web_search_enabled` is True only when the running agent declared the
    `web_search` tool; it drives the top-level `web_search` key in config.toml
    (see `render_config_toml`). Required (no default) because it gates a
    security boundary.
    """
    tmp_root.mkdir(parents=True, exist_ok=True)
    name = f"{_DIR_PREFIX}{os.getpid()}-{secrets.token_hex(4)}"
    home = tmp_root / name
    home.mkdir(mode=0o700)
    # mkdir(mode=…) honors umask; re-chmod to be sure.
    home.chmod(stat.S_IRWXU)
    config_text = render_config_toml(
        host=host, port=port, bearer_env_var=bearer_env_var,
        web_search_enabled=web_search_enabled,
    )
    cfg = home / "config.toml"
    cfg.write_text(config_text)
    cfg.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return home


# ── detect_auth_mode ────────────────────────────────────────────────────────

AuthMode = Literal["chatgpt", "apikey"]


def detect_auth_mode(
    *, canonical_path: Path, env: Mapping[str, str],
) -> AuthMode | None:
    """Return `"chatgpt"` / `"apikey"` per the canonical auth.json file, or
    None when no usable auth exists.

    Reading order (n-3):
      1. `auth.json`'s `auth_mode` field — **authoritative** when present.
      2. Fallback: key-presence in the file body or `OPENAI_API_KEY` env var.

    A misread sends a user down the wrong credential path (copy-in +
    propagate-back vs API-key write), which under M3's monotonic guard could
    touch canonical when it never should. Caller routes:
      - "chatgpt" → copy-in + propagate-changed-back at teardown.
      - "apikey"  → write a fresh 0600 auth.json into CODEX_HOME from the key.
      - None      → raise an `auth` ProviderError before SDK options are built.
    """
    payload: dict[str, object] | None = None
    if canonical_path.exists():
        try:
            payload = json.loads(canonical_path.read_text())
        except (OSError, ValueError):
            payload = None
    if isinstance(payload, dict):
        mode = payload.get("auth_mode")
        if mode in ("chatgpt", "apikey"):
            return mode
        # Field absent: corroborate via key-presence.
        if payload.get("OPENAI_API_KEY") or env.get("OPENAI_API_KEY"):
            return "apikey"
        if payload.get("tokens"):
            return "chatgpt"
    # No file: env-only fallback.
    if env.get("OPENAI_API_KEY"):
        return "apikey"
    return None


# ── seed_auth ────────────────────────────────────────────────────────────────

def seed_auth(
    *, mode: AuthMode, canonical_path: Path, codex_home: Path,
    env: Mapping[str, str],
) -> Path:
    """Seed a `0600 auth.json` into `codex_home` per the auth mode.

    - "chatgpt": copy the canonical file in (so CLI sees a real ChatGPT-login
      auth bundle). The propagate-back step at teardown (see propagate_back)
      reconciles any refresh under flock + monotonic guard.
    - "apikey": write a minimal auth.json carrying the env key. No
      propagate-back, no flock — there's no canonical mutation path.
    """
    target = codex_home / "auth.json"
    if mode == "chatgpt":
        shutil.copyfile(canonical_path, target)
    else:
        key = env.get("OPENAI_API_KEY", "")
        target.write_text(json.dumps({
            "auth_mode": "apikey",
            "OPENAI_API_KEY": key,
        }))
    target.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return target


# ── propagate_back ───────────────────────────────────────────────────────────

PropagateResult = Literal["no_refresh", "written", "stale_skipped", "lock_busy"]


def _last_refresh(payload: dict[str, object]) -> str:
    """Pull `tokens.last_refresh` as an ISO-8601 string; missing/non-string → "".
    Lexicographic compare on ISO-8601 UTC strings is monotonic so we don't
    need to parse to datetimes here (H2 guard)."""
    tokens = payload.get("tokens")
    if isinstance(tokens, dict):
        lr = tokens.get("last_refresh")
        if isinstance(lr, str):
            return lr
    return ""


def _acquire_lock_nonblocking(
    lock_path: Path, *, timeout_seconds: float,
) -> int | None:
    """Bounded LOCK_NB retry loop. Returns the held fd, or None on timeout.
    Stale-lock recovery is automatic: flock is fd-tied, so a crashed holder's
    lock releases on fd close (no reaper needed)."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # 0600 mode on creation (only the owning user should see this file).
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except OSError as exc:
            if exc.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                os.close(fd)
                raise
            if time.monotonic() >= deadline:
                os.close(fd)
                return None
            time.sleep(0.05)


def propagate_back(
    *, canonical_path: Path, codex_home_auth: Path, lock_path: Path,
    timeout_seconds: float = 2.0,
) -> PropagateResult:
    """ChatGPT-login teardown: write the rotated token back to canonical iff
    CODEX_HOME's `last_refresh` is strictly newer than canonical's *current*
    `last_refresh` (re-read under the lock — monotonic guard, M3).

    Atomicity (m-1): write-temp-in-canonical-dir + `os.replace()`. A reader
    (sibling run's copy-in or the user's own codex) always observes the
    complete old-or-new file, never a half-written one.

    Lock (n-4): `~/.codex/.imp-auth.lock` (advisory `fcntl.flock(LOCK_EX)`,
    bounded `LOCK_NB` retry). Stale-lock recovery is automatic. Returns:
      - "no_refresh"    — CODEX_HOME's last_refresh ≤ canonical's; nothing to do.
      - "written"       — guard passed; canonical now holds CODEX_HOME's contents.
      - "stale_skipped" — CODEX_HOME's last_refresh < canonical's (concurrent
                          sibling advanced canonical after our copy-in).
      - "lock_busy"     — bounded LOCK_NB retry expired; skip and let the next
                          run reconcile. (Caller still shreds — n-5.)
    """
    home_payload = json.loads(codex_home_auth.read_text())
    home_lr = _last_refresh(home_payload)
    # Cheap pre-check under no lock (canonical writes are atomic — m-1).
    canon_payload = (
        json.loads(canonical_path.read_text()) if canonical_path.exists() else {}
    )
    canon_lr = _last_refresh(canon_payload)
    if home_lr == canon_lr:
        # CODEX_HOME wasn't refreshed relative to canonical — nothing to do.
        return "no_refresh"
    if home_lr < canon_lr:
        # A concurrent sibling already advanced canonical past us.
        return "stale_skipped"
    fd = _acquire_lock_nonblocking(lock_path, timeout_seconds=timeout_seconds)
    if fd is None:
        return "lock_busy"
    try:
        # Re-read canonical under the lock so a sibling's intervening refresh
        # is seen here (M3 monotonic guard).
        canon_payload = (
            json.loads(canonical_path.read_text())
            if canonical_path.exists() else {}
        )
        if home_lr <= _last_refresh(canon_payload):
            return "stale_skipped"
        # Atomic write-temp + os.replace into the canonical dir.
        tmp_fd, tmp_path = tempfile.mkstemp(
            prefix=canonical_path.name + ".",
            dir=str(canonical_path.parent),
        )
        try:
            with os.fdopen(tmp_fd, "w") as tf:
                tf.write(json.dumps(home_payload))
            os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR)
            os.replace(tmp_path, canonical_path)
        except BaseException:
            # Best-effort temp cleanup on failure mid-write.
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            raise
        return "written"
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# ── teardown_codex_home ──────────────────────────────────────────────────────

def _shred_best_effort(path: Path) -> None:
    """Best-effort defense-in-depth (L5): `shred -u` if available, fall back
    to overwrite-then-unlink. Frame it as best-effort, not a guarantee — it
    doesn't reliably overwrite on tmpfs / CoW filesystems."""
    shred = shutil.which("shred")
    if shred is not None:
        try:
            subprocess.run([shred, "-u", "-n", "1", str(path)],
                           check=True, timeout=5.0,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        except (OSError, subprocess.SubprocessError):
            pass
    # Fallback: overwrite once with zeros, then unlink.
    try:
        with open(path, "r+b") as f:
            size = f.seek(0, 2)
            f.seek(0)
            f.write(b"\x00" * size)
            f.flush()
            os.fsync(f.fileno())
    except OSError:
        pass
    with contextlib.suppress(OSError):
        path.unlink()


def teardown_codex_home(
    *, codex_home: Path, mode: AuthMode | None, canonical_path: Path,
    lock_path: Path, propagate_timeout_seconds: float = 2.0,
) -> None:
    """Tear down a per-run CODEX_HOME.

    Ordering (n-5):
      1. For "chatgpt" mode: attempt propagate-back (may no-op when no refresh
         happened or when LOCK_NB retry expires).
      2. Always: shred-then-unlink `auth.json` regardless of whether step 1
         wrote anything — a skipped propagate-back must NOT leave the
         copied-in token un-shredded.
      3. Always: remove the rest of the dir tree.

    Idempotent against a partial teardown (a previous run that died mid-step)
    via `missing_ok=True` on the unlinks.
    """
    auth = codex_home / "auth.json"
    if mode == "chatgpt" and auth.exists():
        try:
            propagate_back(
                canonical_path=canonical_path, codex_home_auth=auth,
                lock_path=lock_path, timeout_seconds=propagate_timeout_seconds,
            )
        except Exception as exc:
            # Unexpected propagate-back failure (not lock-contention — that returns
            # "lock_busy"). Log and continue to shred unconditionally (n-5).
            _logger.warning(
                "codex: propagate_back failed unexpectedly; shredding local copy: %s", exc
            )
    if auth.exists():
        _shred_best_effort(auth)
    # Best-effort recursive remove of the rest.
    if codex_home.exists():
        shutil.rmtree(codex_home, ignore_errors=True)


# ── sweep_stale_codex_homes ──────────────────────────────────────────────────

_DIR_RE = _re.compile(r"^imp-codex-(\d+)-[0-9a-f]+$")


def _pid_alive(pid: int) -> bool:
    """signal 0 (POSIX): succeeds iff the process exists and we can signal
    it; ESRCH ⇒ no such process; EPERM ⇒ exists but owned by another user
    (treat as alive — we won't sweep it)."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def sweep_stale_codex_homes(
    *, tmp_root: Path, max_age_seconds: float,
) -> None:
    """Best-effort cleanup of orphan per-run CODEX_HOME dirs (L4).

    Skip rules:
      - Dir name doesn't match our prefix → ignore.
      - Encoded pid is still alive → skip (the dir is in-use; the age bound's
        value stops being load-bearing).
      - Encoded pid is dead AND mtime older than `max_age_seconds` → remove.

    Runs on startup and opportunistically; idempotent and exception-safe so a
    permission error on one dir never aborts the sweep.
    """
    if not tmp_root.exists():
        return
    now = time.time()
    for entry in tmp_root.iterdir():
        if not entry.is_dir():
            continue
        m = _DIR_RE.match(entry.name)
        if m is None:
            continue
        pid = int(m.group(1))
        if _pid_alive(pid):
            continue
        try:
            age = now - entry.stat().st_mtime
        except OSError:
            continue
        if age < max_age_seconds:
            continue
        shutil.rmtree(entry, ignore_errors=True)
