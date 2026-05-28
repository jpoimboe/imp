"""Dogfood end-to-end test: imp on imp's own source.

This is the integration test that proves the shipped default workflow runs
end-to-end against a real LLM on a real (non-trivial) project — imp's own
source tree. The coder agent makes a small, deterministic edit (a sentinel
print at the top of the bash tool's function); we verify the workflow ran
to completion, all events fired correctly, and the sentinel landed in the
expected file.

What this proves:
  1. The shipped workflow tree (`workflow.yaml`, `scripts/`, agents) runs
     against real code, not an empty tmp_path.
  2. `scripts/run-tests.sh` actually exercises a real `make test`.
  3. The reviewer reviews a real diff.
  4. The driver decides when to act based on engine-emitted events,
     not by matching renderer output strings.
  5. Every yaml-declared human gate gets answered exactly once.
  6. `imp log` and `imp log --full` smoke-pass.
  7. The coder's specific code change landed (sentinel grep).

Skip predicates (all must pass):
  - first_opted_in() is not None  (IMP_LIVE_PROVIDERS opt-in)
  - IMP_RUN_DOGFOOD=1             (cost guard; set by `make test-dogfood`)
  - _can_pty()                    (Windows / sandboxed CI without /dev/pty*)

Recursion guard: the coder runs `make test` -> `pytest -q` in the copied
tree, which would re-collect this file. Scrubbing IMP_LIVE_PROVIDERS from
the subprocess env causes all live tests to skip via opted_in().
"""

from __future__ import annotations

import contextlib
import json
import os
import select
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests._live import first_opted_in


# ── Constants ──────────────────────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).resolve().parents[1]


# ── Helpers ────────────────────────────────────────────────────────────────


def _anthropic_key() -> str | None:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key
    key_file = Path.home() / ".anthropic-key"
    if key_file.exists():
        return key_file.read_text().strip()
    return None


def _openai_key() -> str | None:
    key = os.environ.get("OPENAI_API_KEY")
    if key:
        return key
    key_file = Path.home() / ".openai-key"
    if key_file.exists():
        return key_file.read_text().strip()
    return None


def _provider_model(provider: str) -> str:
    return {
        "anthropic": "claude-sonnet-4-6",
        "openai": "gpt-4o-mini",
        "claude_code": "claude-sonnet-4-5",
    }[provider]


def _can_pty() -> bool:
    try:
        import pty
        m, s = pty.openpty()
        os.close(m)
        os.close(s)
        return True
    except Exception:
        return False


def _walk_yaml_human_gates(wf_yaml: Path) -> list[tuple[str, int]]:
    """Return [(phase_name, gate_index), ...] for every human gate declared
    in workflow.yaml. The driver uses len(...) to know how many human gates
    should fire end-to-end."""
    data = yaml.safe_load(wf_yaml.read_text())
    out: list[tuple[str, int]] = []
    for phase in data.get("phases", []):
        gates = (data.get(phase) or {}).get("gates") or []
        for idx, g in enumerate(gates):
            if g.get("type") == "human":
                out.append((phase, idx))
    return out


def _ignore_for_copy(_dir: str, names: list[str]) -> set[str]:
    return {n for n in names
            if n in {".venv", ".git", ".imp", ".pytest_cache",
                     ".mypy_cache", ".ruff_cache", ".coverage",
                     "htmlcov", ".tox", "__pycache__", "dist", "build"}
            or n.endswith(".egg-info")}


def _seed_dogfood_project(tmp_path: Path, provider: str) -> None:
    """Copy imp source into tmp_path, init git, bootstrap .imp/ via run_init."""
    shutil.copytree(PROJECT_ROOT, tmp_path, ignore=_ignore_for_copy,
                    dirs_exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        ["git",
         "-c", "user.email=test@imp",
         "-c", "user.name=imp-dogfood",
         "commit", "-q", "-m", "initial"],
        cwd=tmp_path, check=True,
    )

    # Bootstrap .imp/ via the real init code path with injected I/O.
    # (`imp init` has no flags — it always launches a TUI picker and
    # prompts for env-var names. run_init accepts callbacks so callers
    # can drive it without a TTY.)
    from imp.cli.init import run_init
    run_init(
        project_root=tmp_path,
        input_fn=lambda _prompt: "",
        output_fn=lambda _line: None,
        picker_fn=lambda _available: [provider],
    )


# ── Skip predicates ────────────────────────────────────────────────────────

pytestmark = [
    pytest.mark.skipif(
        first_opted_in() is None,
        reason="no provider in IMP_LIVE_PROVIDERS",
    ),
    pytest.mark.skipif(
        os.environ.get("IMP_RUN_DOGFOOD") != "1",
        reason="dogfood run on real LLM; set IMP_RUN_DOGFOOD=1 to opt in",
    ),
    pytest.mark.skipif(
        not _can_pty(),
        reason="pty not available (Windows or sandboxed CI)",
    ),
]


# ── Test ───────────────────────────────────────────────────────────────────


def test_dogfood_imp_on_imp_default_workflow(tmp_path: Path) -> None:
    """Placeholder body — Task 2 fills in the real test."""
    pytest.skip("dogfood body not yet implemented (Task 2)")
