"""Read-only `imp config` provenance resolver (spec §5). Never mutates."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TextIO

from imp.providers.effective import FLOOR_SRC, EffectiveConfig
from imp.providers.resolve import iter_provider_defaults
from imp.remote.drivers import REGISTRY


def _label(src_file: Path) -> str:
    """Human label for a leaf's source file (the right-hand side of `←`)."""
    if src_file == FLOOR_SRC.file:
        return "provider default"
    name = src_file.name
    if name == "project.yaml":
        return f"{src_file} (project)"
    if name == "config.yaml" and src_file.parent.name == ".imp":
        return f"{src_file} (local)"
    return f"{src_file} (user)"


def _arrow(out: TextIO, key: str, value: str, src_file: Path) -> None:
    out.write(f"  {key:<28} {value:<24} ← {_label(src_file)}\n")


def render_config(eff: EffectiveConfig, out: TextIO, *, color: bool = False,
                  show_all: bool = False) -> None:
    cfg, prov = eff.config, eff.provenance
    out.write("tiers:\n")
    configured = set(cfg.providers)
    for tier in sorted(cfg.tiers):
        for vendor in sorted(cfg.tiers[tier]):
            if not show_all and configured and vendor not in configured:
                continue
            src = prov.get(("tiers", tier, vendor), FLOOR_SRC)
            _arrow(out, f"{tier} {vendor}", cfg.tiers[tier][vendor], src.file)
    out.write("\nsettings:\n")
    for key, val in (
        ("providers", ", ".join(cfg.providers) or "(none)"),
        ("theme", cfg.theme),
        ("editing_mode", cfg.editing_mode),
        ("lint_command", cfg.lint_command or "(unset)"),
        ("test_command", cfg.test_command or "(unset)"),
    ):
        leaf_src = prov.get((key,))
        label = _label(leaf_src.file) if leaf_src is not None else "default"
        out.write(f"  {key:<28} {val:<24} ← {label}\n")

    out.write("\nprovider keys (local check):\n")
    defaults = iter_provider_defaults()
    for vendor in cfg.providers:
        d = defaults.get(vendor)
        # CLI-authenticated providers (claude_code, codex) declare env_var=None
        # in the floor — they authenticate via `claude login` / `codex login`,
        # not an API key. Don't fabricate a `CLAUDE_CODE_API_KEY not set` check
        # (env_for would invent that name) for a provider that's correctly set up.
        # An explicit provider_envs override means the operator opted into a key.
        if d is not None and d.env_var is None and vendor not in cfg.provider_envs:
            out.write(f"  ✓ {vendor:<14} (CLI-authenticated)\n")
            continue
        env = cfg.env_for(vendor)
        present = bool(os.environ.get(env))
        mark = "✓" if present else "✗"
        suffix = "set" if present else f"{env} not set"
        out.write(f"  {mark} {vendor:<14} {suffix}\n")

    out.write("\navailable drivers:\n")
    enabled = (cfg.remote.get("driver") or {}).get("name")
    for name in sorted(REGISTRY):
        entry = REGISTRY[name]
        state = "enabled" if name == enabled else "available"
        out.write(f"  {name:<14} ({state})\n")
        out.write(f"      {entry.description}\n")
        out.write(f"      env: {', '.join(entry.required_env)}\n")


def render_explain(eff: EffectiveConfig, tier: str, out: TextIO, *, color: bool = False) -> None:
    cfg, prov = eff.config, eff.provenance
    if tier not in cfg.tiers:
        out.write(f"unknown tier {tier!r} (known: {sorted(cfg.tiers)})\n")
        return
    out.write(f"tier {tier}:\n")
    for vendor in sorted(cfg.tiers[tier]):
        src = prov.get(("tiers", tier, vendor), FLOOR_SRC)
        _arrow(out, vendor, cfg.tiers[tier][vendor], src.file)
