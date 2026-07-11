"""Floor + three-layer config merge with per-leaf provenance (spec
2026-06-08-config-file-split §2).

Precedence (later wins):
    provider code (floor) ← imp/project.yaml ← ~/.config/imp/config.yaml
                          ← .imp/config.yaml (control root, gitignored)

The merged `Config.tiers` is always fully populated (floor + overrides), so
`resolve.resolve_run` reads it unchanged. Provenance is a side-table keyed by
leaf-path — NEVER folded into `config.tiers`, whose leaves stay plain `str`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from imp.engine.types import SourceLoc
from imp.providers.config import Config, parse_config_layer, parse_project_facts
from imp.providers.resolve import iter_provider_defaults

# A synthetic source for floor leaves. The string is what `imp config` prints.
FLOOR_SRC = SourceLoc(file=Path("<provider code>"), line=1)

LeafPath = tuple[str, ...]
Provenance = dict[LeafPath, SourceLoc]


@dataclass(frozen=True)
class EffectiveConfig:
    config: Config
    provenance: Provenance


def user_config_path() -> Path:
    """$XDG_CONFIG_HOME/imp/config.yaml, default ~/.config/imp/config.yaml."""
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "imp" / "config.yaml"


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


def _to_config(
    merged: dict[str, Any], project_facts: dict[str, str], guidance: str,
) -> Config:
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
        max_parallel_test_runs=merged.get("max_parallel_test_runs", 1),
        editing_mode=merged.get("editing_mode", "emacs"),
        theme=merged.get("theme", "ansi"),
        sandbox=merged.get("sandbox", {}),
        remote=merged.get("remote", {}),
        context=merged.get("context", {}),
        project_facts=project_facts,
        guidance=guidance,
    )


def load_effective_config(
    *, project_root: Path | None = None, control_root: Path | None = None,
) -> EffectiveConfig:
    """Build the floor, then merge project ← user ← local on top.

    - project layer: `project_root/imp/project.yaml` (the worktree for a Quest;
      the control-root checkout for Quest-less commands). Skipped when
      project_root is None.
    - user layer: `user_config_path()` (live).
    - local layer: `control_root/.imp/config.yaml` (live, gitignored). Skipped
      when control_root is None.
    """
    layers: list[tuple[dict[str, Any], SourceLoc]] = [_floor_layer()]
    project_facts: dict[str, str] = {}
    guidance = ""
    if project_root is not None:
        proj_path = project_root / "imp" / "project.yaml"
        p = parse_config_layer(proj_path)
        if p is not None:
            layers.append(p)
        project_facts, guidance = parse_project_facts(proj_path)
    u = parse_config_layer(user_config_path())
    if u is not None:
        layers.append(u)
    if control_root is not None:
        local = parse_config_layer(control_root / ".imp" / "config.yaml")
        if local is not None:
            layers.append(local)
    merged, prov = merge_layers(layers)
    return EffectiveConfig(config=_to_config(merged, project_facts, guidance), provenance=prov)
