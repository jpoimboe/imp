# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""Floor + three-layer config merge with per-leaf provenance (spec
2026-06-08-config-file-split §2).

Precedence (later wins):
    provider code (floor) ← <control root>/imp/config.yaml
                          ← ~/.imp/config.yaml
                          ← ~/.imp/projects/<name>/config.yaml (this checkout)

The project layer lives in the MAIN CHECKOUT and the local layer in that
checkout's own state directory (`~/.imp/config.yaml` is machine-wide): a quest
worktree has no say in the settings its own gates are judged by.

The merged `Config.tiers` is always fully populated (floor + overrides), so
`resolve.resolve_run` reads it unchanged. Provenance is a side-table keyed by
leaf-path — NEVER folded into `config.tiers`, whose leaves stay plain `str`.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from imp.engine.errors import LoadError
from imp.engine.types import SourceLoc
from imp.paths import project_state_dir
from imp.providers.config import Config, parse_config_layer
from imp.providers.memory import validate_memory_tree
from imp.providers.paths import global_config_path, project_rename_pending
from imp.providers.resolve import iter_provider_defaults

# A synthetic source for floor leaves. The string is what `imp config` prints.
FLOOR_SRC = SourceLoc(file=Path("<provider code>"), line=1)

LeafPath = tuple[str, ...]
Provenance = dict[LeafPath, SourceLoc]


@dataclass(frozen=True)
class EffectiveConfig:
    config: Config
    provenance: Provenance


def project_rename_hint(root: Path) -> str:
    """The rename instruction, naming the checkout and (when resolvable) its branch."""
    from imp.engine.worktree import checked_out_branch

    branch = checked_out_branch(root)
    where = f"{root} (branch {branch})" if branch is not None else str(root)
    return (f"imp/project.yaml found in {where} but imp now reads imp/config.yaml — "
            f"rename it with `git -C {root} mv imp/project.yaml imp/config.yaml`")


def _floor_layer() -> tuple[dict[str, Any], SourceLoc]:
    """The bottom merge layer, materialized from registry metadata: every
    registered vendor's tier_defaults assembled into {tier: {vendor: model}},
    plus conventional provider_envs."""
    tiers: dict[str, dict[str, str]] = {}
    envs: dict[str, str] = {}
    for name, d in iter_provider_defaults().items():
        if d.tier_defaults is None:
            continue
        for tier, model in d.tier_defaults.items():
            tiers.setdefault(tier, {})[name] = model
        if d.env_var is not None:
            envs[name] = d.env_var
    layer: dict[str, Any] = {"tiers": tiers}
    if envs:
        layer["provider_envs"] = envs
    return layer, FLOOR_SRC


def merge_layers(
    layers: list[tuple[dict[str, Any], SourceLoc]],
) -> tuple[dict[str, Any], Provenance]:
    """Recursive structural merge, leaves replace (spec §2). `layers` is ordered
    low→high precedence. `tiers` deep-merges two levels; `provider_envs`/`vars`/`sandbox`/`context`
    deep-merge one level; everything else is a leaf replaced wholesale."""
    out: dict[str, Any] = {}
    prov: Provenance = {}
    for layer, src in layers:
        for key, val in layer.items():
            if key == "tiers":
                dst: dict[str, Any] = out.setdefault("tiers", {})
                for tier, vendors in val.items():
                    td: dict[str, str] = dst.setdefault(tier, {})
                    for vendor, model in vendors.items():
                        td[vendor] = model
                        prov[("tiers", tier, vendor)] = src
            elif key in ("provider_envs", "vars", "sandbox", "context"):
                dst2: dict[str, Any] = out.setdefault(key, {})
                for k, v in val.items():
                    dst2[k] = v
                    prov[(key, k)] = src
            else:  # leaf: providers, theme, editing_mode, test_cmd_*
                out[key] = val
                prov[(key,)] = src
    return out, prov


def _pythonpath(raw: Any) -> tuple[str, ...] | Literal[False] | None:
    """False and None are answers, not sequences; only a list becomes a tuple."""
    return tuple(raw) if isinstance(raw, list) else raw


def _to_config(merged: dict[str, Any]) -> Config:
    return Config(
        providers=tuple(merged.get("providers", ())),
        tiers=merged.get("tiers", {}),
        provider_envs=merged.get("provider_envs", {}),
        source=FLOOR_SRC,
        var_overrides=merged.get("vars", {}),
        test_cmd_fast=merged.get("test_cmd_fast"),
        test_cmd_slow=merged.get("test_cmd_slow"),
        test_cmd_fast_timeout=merged.get("test_cmd_fast_timeout"),
        test_cmd_slow_timeout=merged.get("test_cmd_slow_timeout"),
        worktree_setup=merged.get("worktree_setup"),
        pythonpath=_pythonpath(merged.get("pythonpath")),
        max_parallel_test_runs=merged.get("max_parallel_test_runs", 1),
        editing_mode=merged.get("editing_mode", "emacs"),
        theme=merged.get("theme", "ansi"),
        default_workflow=merged.get("default-workflow", "default"),
        sandbox=merged.get("sandbox", {}),
        remote=merged.get("remote", {}),
        context=merged.get("context", {}),
    )


def floor_config() -> Config:
    """The provider-code floor alone — no project, global, or local layer."""
    return _to_config(merge_layers([_floor_layer()])[0])


def load_effective_config(*, control_root: Path | None = None) -> EffectiveConfig:
    """Build the floor, then merge project ← global ← local on top.

    - project layer: `control_root/imp/config.yaml` — the main checkout, never a
      quest worktree. Skipped when control_root is None; raises when only the
      pre-rename `imp/project.yaml` is present.
    - global layer: `global_config_path()` (live).
    - local layer: `project_state_dir(control_root)/config.yaml` (live).

    control_root=None is floor + global only (`cli/pythonpath_detect.py` and the
    quest-less subagent fallback rely on that).
    """
    layers: list[tuple[dict[str, Any], SourceLoc]] = [_floor_layer()]
    if control_root is not None:
        if project_rename_pending(control_root):
            raise LoadError(project_rename_hint(control_root))
        p = parse_config_layer(control_root / "imp" / "config.yaml")
        if p is not None:
            layers.append(p)
    g = parse_config_layer(global_config_path())
    if g is not None:
        layers.append(g)
    if control_root is not None:
        local = parse_config_layer(project_state_dir(control_root) / "config.yaml")
        if local is not None:
            layers.append(local)
    merged, prov = merge_layers(layers)
    return EffectiveConfig(config=_to_config(merged), provenance=prov)


def resolve_default_workflow(*, control_root: Path | None) -> str:
    """The layered `default-workflow` for one settings root."""
    return load_effective_config(control_root=control_root).config.default_workflow


def memory_validation_scopes(
    project_root: Path,
    relpaths: Iterable[str],
) -> tuple[list[str], dict[str, frozenset[str]]]:
    """Build the (valid_workflows, workflow_steps) inputs for
    `validate_memory_tree`: every known workflow name, plus the step names of
    each workflow referenced by a `<workflow>/...` relpath."""
    from imp.assets import available_workflow_names, resolve_workflow_dir
    from imp.engine.loader import load_workflow

    valid = available_workflow_names(project_root)
    valid_set = set(valid)
    referenced = sorted(
        {
            key.split("/", 1)[0]
            for key in relpaths
            if "/" in key and key.split("/", 1)[0] in valid_set
        }
    )
    workflow_steps: dict[str, frozenset[str]] = {}
    for name in referenced:
        wf = load_workflow(resolve_workflow_dir(project_root, name))
        workflow_steps[name] = frozenset(s.name for s in wf.flat_steps())
    return valid, workflow_steps


def validate_project_memory(
    project_root: Path,
    memory: dict[str, str],
) -> None:
    """Validate a loaded `imp/memory/` snapshot against the project's known
    workflows and their steps. Raises LoadError (loud, fail-fast) on any unknown
    workflow dir, non-step `.md`, or stray top-level step file. An empty
    snapshot is a no-op. Loads only the workflows a subdir references."""
    if not memory:
        return
    valid, workflow_steps = memory_validation_scopes(project_root, memory.keys())
    errors = validate_memory_tree(
        memory.keys(), valid_workflows=valid, workflow_steps=workflow_steps
    )
    if errors:
        raise LoadError(
            "invalid imp/memory/ tree:\n  " + "\n  ".join(errors),
            SourceLoc(file=project_root / "imp" / "memory", line=1),
        )
