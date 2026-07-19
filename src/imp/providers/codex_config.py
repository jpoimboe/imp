"""Per-run CODEX_HOME provisioning + auth handling (spec §3, design "run() flow"
step 3, "Lifecycle / teardown", and "Auth copy-in").

Implements what the SDK can't express (or can't express correctly): the MCP
server URL + bearer-token env-var name, the [features] toggles, the top-level
`web_search` control, and the auth.json seed. `web_search` lives here because the
SDK's `features.web_search_request` flag is deprecated/ineffective on codex
>=0.137 (see `render_config_toml`). The rest (model, sandbox_mode,
approval_policy, network_access_enabled) still goes through SDK options.

Filesystem invariants:
  - CODEX_HOME dir is mode 0700 under TMPDIR, with the owner pid encoded in
    the dir name so the startup sweep can skip live-owner dirs (L4).
  - auth.json and config.toml are mode 0600.
"""

from __future__ import annotations

import contextlib
import json
import os
import re as _re
import secrets
import shutil
import stat
import subprocess
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

# ── render_config_toml ──────────────────────────────────────────────────────

def render_config_toml(
    *, host: str, port: int, bearer_env_var: str, web_search_enabled: bool,
    impty_url: str | None = None, impty_bearer_env_var: str | None = None,
) -> str:
    """Emit the `config.toml` text for a per-run CODEX_HOME.

    Carries the imp MCP server entry, the [features] toggles, the top-level
    `web_search` control, and — when the agent holds the impty driver — a second
    in-process HTTP MCP server block for impty (url + bearer-token env var). impty
    is hosted in-process on its own ephemeral port, identical in shape to the imp
    server, NOT as a stdio command/args block.

    `web_search` lives here (not in SDK options) because codex >=0.137 deprecated
    the SDK-emitted `features.web_search_request` key. A top-level key must precede
    every `[table]` in TOML, so `web_search` is emitted first.
    """
    web_search = "live" if web_search_enabled else "disabled"
    impty_block = ""
    if impty_url is not None and impty_bearer_env_var is not None:
        impty_block = (
            "\n"
            "[mcp_servers.impty]\n"
            f'url = "{impty_url}"\n'
            f'bearer_token_env_var = "{impty_bearer_env_var}"\n'
        )
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
        + impty_block
    )


# ── provision_codex_home ────────────────────────────────────────────────────

_DIR_PREFIX = "imp-codex-"


def provision_codex_home(
    *, tmp_root: Path, host: str, port: int, bearer_env_var: str,
    web_search_enabled: bool, impty_url: str | None = None,
    impty_bearer_env_var: str | None = None,
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
        web_search_enabled=web_search_enabled, impty_url=impty_url,
        impty_bearer_env_var=impty_bearer_env_var,
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

    A misread sends a user down the wrong credential path (copy-in vs API-key
    write). Caller routes:
      - "chatgpt" → copy the canonical auth.json into CODEX_HOME.
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

    - "chatgpt": copy the canonical file in (so the CLI sees a real
      ChatGPT-login auth bundle). imp keeps canonical fresh pre-spawn; this
      copy is a throwaway shredded at teardown.
    - "apikey": write a minimal auth.json carrying the env key.
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
) -> None:
    """Tear down a per-run CODEX_HOME: shred `auth.json` (best-effort,
    unconditional) then remove the dir tree.

    imp keeps the canonical token fresh pre-spawn, so the copied-in CODEX_HOME
    credential is a pure throwaway — there is no rotated token to reconcile
    back to canonical here. `mode` / `canonical_path` are retained for
    call-site compatibility; teardown no longer branches on them.

    Idempotent against a partial teardown (a previous run that died mid-step):
    the shred is best-effort and rmtree uses `ignore_errors`.
    """
    auth = codex_home / "auth.json"
    if auth.exists():
        _shred_best_effort(auth)
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
