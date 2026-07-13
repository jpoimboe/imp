"""Helpers for parsing IMP_LIVE_PROVIDERS in live-API tests.

Live tests opt in via the IMP_LIVE_PROVIDERS env var (comma-separated).
Each test's skipif checks `opted_in(<provider>)`. Auth presence is then
asserted (not skipped) — opt-in implies the user has set up credentials.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

KNOWN_PROVIDERS: tuple[str, ...] = ("anthropic", "openai", "claude_code", "codex")

# Sentinel stored as the --run-live option's `const` (bare flag => all providers).
RUN_LIVE_ALL = object()


def parse_run_live(value: object) -> frozenset[str] | None:
    """Resolve the raw --run-live option value into the requested provider set.

    None  -> flag not passed (skip every live test).
    RUN_LIVE_ALL (bare --run-live) -> all known providers.
    "a,b" -> that subset; an unknown token is a pytest.UsageError.
    """
    if value is None:
        return None
    if value is RUN_LIVE_ALL:
        return frozenset(KNOWN_PROVIDERS)
    assert isinstance(value, str)
    tokens = [tok.strip() for tok in value.split(",") if tok.strip()]
    unknown = [tok for tok in tokens if tok not in KNOWN_PROVIDERS]
    if unknown:
        raise pytest.UsageError(
            "--run-live: unknown provider(s): "
            + ", ".join(unknown)
            + "; valid providers: "
            + ", ".join(KNOWN_PROVIDERS)
        )
    return frozenset(tokens)


def anthropic_key() -> str | None:
    """Resolve the anthropic key file-or-env: ~/.anthropic-key before the env
    var, so the conftest "sk-test" stub never shadows a real key."""
    key_file = Path.home() / ".anthropic-key"
    if key_file.exists():
        val = key_file.read_text().strip()
        if val:
            return val
    return os.environ.get("ANTHROPIC_API_KEY") or None


def openai_key() -> str | None:
    """Resolve the openai key file-or-env: ~/.openai-key before the env var."""
    key_file = Path.home() / ".openai-key"
    if key_file.exists():
        val = key_file.read_text().strip()
        if val:
            return val
    return os.environ.get("OPENAI_API_KEY") or None


def provider_available(provider: str) -> str | None:
    """None if the provider's toolchain + credentials are present on this
    machine, else a human-readable reason naming what is missing."""
    if provider == "claude_code":
        return None if shutil.which("claude") else "claude CLI not on PATH"
    if provider == "codex":
        if shutil.which("codex") is None:
            return "codex CLI not on PATH"
        if importlib.util.find_spec("openai_codex_sdk") is None:
            return "openai_codex_sdk not importable"
        return None
    if provider == "anthropic":
        return None if anthropic_key() else (
            "anthropic credentials not found "
            "(set ANTHROPIC_API_KEY or create ~/.anthropic-key)"
        )
    if provider == "openai":
        return None if openai_key() else (
            "openai credentials not found "
            "(set OPENAI_API_KEY or create ~/.openai-key)"
        )
    raise ValueError(f"unknown provider: {provider!r}")


def live_skip_reason(requested: frozenset[str] | None, provider: str) -> str | None:
    """Two-layer gate for one live-marked test. Returns a skip reason, or None
    to run. Layer 1: no --run-live -> skip all. Layer 2: not in the requested
    set -> skip as not-selected; requested but toolchain/creds missing -> skip
    with the capability reason."""
    if requested is None:
        return "live-API test — pass --run-live to run"
    if provider not in requested:
        return f"{provider} not selected by --run-live"
    return provider_available(provider)


def _parse() -> list[str]:
    raw = os.environ.get("IMP_LIVE_PROVIDERS", "")
    return [tok.strip() for tok in raw.split(",") if tok.strip()]


def opted_in(provider: str) -> bool:
    return provider in _parse()


def first_opted_in() -> str | None:
    items = _parse()
    return items[0] if items else None


def assert_codex_toolchain() -> None:
    """Assert the codex CLI binary and SDK are both present.

    Opting codex into IMP_LIVE_PROVIDERS implies the toolchain is installed, so
    this asserts (fails loudly) rather than skipping — mirroring how the other
    providers assert their auth once opted in. `make test-live` validates the
    `codex` binary up front in _REQUIRE_PROVIDERS; this also covers the
    pytest-direct path and the SDK import the Makefile does not check."""
    import importlib.util
    import shutil

    assert shutil.which("codex") is not None, (
        "codex opted in via IMP_LIVE_PROVIDERS but `codex` CLI not on PATH"
    )
    assert importlib.util.find_spec("openai_codex_sdk") is not None, (
        "codex opted in via IMP_LIVE_PROVIDERS but openai_codex_sdk not importable"
    )


def build_isolated_venv(
    project_root: Path,
    *,
    python: str | None = None,
    extras: tuple[str, ...] = (),
) -> Path:
    """Build an ISOLATED venv (NO --system-site-packages) under
    project_root/.venv and install project_root editable + its deps into it.

    Prefers `uv` for speed; falls back to stdlib venv + pip. No
    --system-site-packages means the host's editable `imp` cannot shadow the
    copy: the venv sees only what is installed here.

    `extras` names optional-dependency groups (e.g. ("dev",)) to install with
    the editable target — the isolated venv has no host site-packages to borrow
    from, so test tools like pytest must be installed here explicitly. Empty by
    default so the stub-isolation test keeps a deliberately bare venv.

    Returns the venv's python interpreter path.
    """
    import shutil

    venv_dir = project_root / ".venv"
    base_python = python or sys.executable
    target = (
        f"{project_root}[{','.join(extras)}]" if extras else str(project_root)
    )

    if shutil.which("uv"):
        # uv's cache (~/.cache/uv) and the venv (often tmpfs /tmp) sit on
        # different filesystems, so its default hardlink install can't work —
        # it falls back to copying and prints a "Failed to hardlink files"
        # warning on every seed. Ask for copy mode up front to silence it.
        uv_env = {**os.environ, "UV_LINK_MODE": "copy"}
        subprocess.run(
            ["uv", "venv", "--python", base_python, str(venv_dir)],
            check=True, env=uv_env,
        )
        venv_python = venv_dir / "bin" / "python"
        subprocess.run(
            ["uv", "pip", "install", "--python", str(venv_python),
             "-e", target],
            check=True, env=uv_env,
        )
    else:
        subprocess.run([base_python, "-m", "venv", str(venv_dir)], check=True)
        venv_python = venv_dir / "bin" / "python"
        subprocess.run(
            [str(venv_python), "-m", "pip", "install",
             "-e", target, "-q"],
            check=True,
        )
    return venv_python


def _await_worktree(control_root: Path, timeout: float) -> Path:
    """Poll .imp/worktrees/ for the single Quest worktree dir and return it;
    its .name is the slug. The real guard is the presence of
    .imp/quests/<slug>/quest.json at the control root (scratch/.merge-* dirs
    have no such entry); the leading-dot exclusion is redundant belt-and-suspenders.

    Replaces stdout-parsing of `imp new`'s "Created Quest <slug>:" line:
    sidesteps PTY/PIPE read-timing and yields the slug directly. Raises
    TimeoutError if no Quest worktree appears within `timeout` seconds."""
    root = control_root / ".imp" / "worktrees"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if root.is_dir():
            for p in sorted(root.iterdir()):
                if (p.is_dir() and not p.name.startswith(".")
                        and (control_root / ".imp" / "quests" / p.name / "quest.json").exists()):
                    return p
        time.sleep(0.1)
    raise TimeoutError(
        f"no Quest worktree appeared under {root} within {timeout}s"
    )


def _await_change_dir(control_root: Path, timeout: float) -> Path:
    """Poll .imp/quests/ for the single Quest's permanent home dir and return it;
    its .name is the slug. Guards on quest.json so a half-written dir is skipped.

    Under the deferred-worktree model `imp new` creates an un-materialized draft
    (no branch/worktree) with state+events under <control_root>/.imp/quests/<id>/.
    A workflow that never calls create_and_enter_worktree runs to completion as a
    draft, so tests that used to await a worktree must await the change home here.
    Raises TimeoutError if no Quest home appears within `timeout` seconds."""
    root = control_root / ".imp" / "quests"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if root.is_dir():
            for p in sorted(root.iterdir()):
                if p.is_dir() and (p / "quest.json").exists():
                    return p
        time.sleep(0.1)
    raise TimeoutError(
        f"no Quest home appeared under {root} within {timeout}s"
    )
