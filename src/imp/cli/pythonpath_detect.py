"""Agent-backed detection of a project's import roots for the PYTHONPATH shadow.

At `imp init`, a sandboxed one-shot agent reads the project's declared packaging
and confirms the layout on disk; the reported roots are validated here before
being written to `imp/project.yaml` as the worktree-relative `pythonpath` — dirs
prepended to gate/agent subprocess PYTHONPATH so an editable-installed package
resolves to the worktree under test, not a stale main checkout.
"""

from __future__ import annotations

import functools
import json
from dataclasses import dataclass, replace
from pathlib import Path

import anyio

from imp.agents.detached import (
    DetachedEnvironmentFailure,
    DetachedSuccess,
    DetachedTransientFailure,
    run_detached_agent,
)
from imp.engine.loader import parse_agent_file
from imp.engine.sandbox import SandboxConfig
from imp.providers.effective import load_effective_config
from imp.providers.resolve import available_providers

_PACKAGING_FILES = ("pyproject.toml", "setup.py", "setup.cfg")


def is_python_packaging_project(project_root: Path) -> bool:
    """True when the project declares Python packaging (a pyproject.toml,
    setup.py, or setup.cfg exists). Gates init's agent detection + readiness
    checks; a project without any of these keeps init's non-Python behavior."""
    return any((project_root / name).is_file() for name in _PACKAGING_FILES)


def resolve_detection_provider(
    providers: list[str], *, tier: str = "medium",
) -> tuple[str, str] | None:
    """The first provider in `providers` (pick order) with a usable credential,
    paired with its `tier` model. None when no picked provider is usable — the
    offline credential check for init's readiness gate."""
    cfg = load_effective_config().config
    usable = available_providers(replace(cfg, providers=tuple(providers)))
    if not usable:
        return None
    provider = usable[0]
    return provider, cfg.tiers[tier][provider]


def _normalize_root(entry: str) -> str | None:
    """Normalize one reported root to a worktree-relative form, or None when it
    is malformed (absolute, or escaping the worktree with `..`)."""
    e = entry.strip().replace("\\", "/")
    if e.startswith("/"):
        return None
    e = e.rstrip("/")
    while e.startswith("./"):
        e = e[2:]
    if e in ("", "."):
        return "."
    if ".." in Path(e).parts:
        return None
    return e


def validate_detected_roots(project_root: Path, roots: list[str]) -> list[str]:
    """Normalize and confirm each reported root against disk: existing dirs
    under `project_root` only, no absolute paths, no `..`, de-duplicated. `"."`
    (the repo root) is always valid. Entries that fail are dropped."""
    out: list[str] = []
    for entry in roots:
        norm = _normalize_root(entry)
        if norm is None or norm in out:
            continue
        if (project_root / norm).is_dir():
            out.append(norm)
    return out


_KICKOFF = (
    "Determine the Python import root(s) for the project at the repository root "
    "and report them via complete_step."
)


@dataclass(frozen=True, slots=True)
class DetectedRoots:
    roots: list[str]  # validated, worktree-relative; empty => no importable package


@dataclass(frozen=True, slots=True)
class DetectionFailed:
    kind: str  # "credential" | "transient"
    detail: str


def detect_pythonpath_via_agent(
    project_root: Path,
    workflow_dir: Path,
    provider_name: str,
    model_name: str,
    sandbox_cfg: SandboxConfig,
) -> DetectedRoots | DetectionFailed:
    """Run the bundled init-pythonpath agent and validate its answer against
    disk. Assumes `project_root` is a Python-packaging project (init pre-checks
    with `is_python_packaging_project`)."""
    agent_def = parse_agent_file(workflow_dir / "agents" / "init-pythonpath.md")
    result = anyio.run(functools.partial(
        run_detached_agent,
        agent_def=agent_def,
        provider_name=provider_name,
        model_name=model_name,
        initial_message=_KICKOFF,
        project_root=project_root,
        workflow_dir=workflow_dir,
        sandbox_cfg=sandbox_cfg,
        title="pythonpath detection",
        tier="medium",
        max_tool_calls=25,
        timeout_s=60.0,
    ))
    if isinstance(result, DetachedEnvironmentFailure):
        kind = "credential" if result.category in ("auth", "config") else "transient"
        return DetectionFailed(kind, result.detail)
    if isinstance(result, DetachedTransientFailure):
        return DetectionFailed("transient", f"{result.category}: {result.detail}")
    assert isinstance(result, DetachedSuccess)
    if result.output.get("status") == "Error":
        return DetectionFailed("transient", "packaging unreadable")
    raw = list((result.output.get("pythonpath") or {}).get("roots", []))
    validated = validate_detected_roots(project_root, raw)
    if raw and not validated:
        return DetectionFailed("transient", "detected roots not found on disk")
    return DetectedRoots(validated)


def format_pythonpath_yaml(dirs: list[str]) -> str:
    """Serialize a `pythonpath:` YAML line matching `format_test_cmd_fast_yaml`'s
    json.dumps idiom, e.g. `["src"]` => `pythonpath: ["src"]\\n`."""
    return f"pythonpath: {json.dumps(dirs)}\n"
