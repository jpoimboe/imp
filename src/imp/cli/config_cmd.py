"""Read-only `imp config` provenance resolver (spec §5). Never mutates."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal, TextIO

from imp.cli import palette as pal
from imp.paths import display_path, imp_home
from imp.providers.effective import FLOOR_SRC, EffectiveConfig
from imp.providers.resolve import iter_provider_defaults
from imp.remote.drivers import REGISTRY


def layer_label(src_file: Path) -> str:
    """Short display name for a config layer's file, shared with `imp config set` output."""
    if src_file == FLOOR_SRC.file:
        return "provider default"
    if src_file.is_relative_to(imp_home()):
        return display_path(src_file)
    return f"{src_file.parent.name}/{src_file.name}"


def _annotation(label: str, *, color: bool) -> str:
    """The bracketed provenance marker, dimmed when color is on."""
    text = f"[{label}]"
    return pal.ansi(text, "dim") if color else text


def _emit_section(out: TextIO, rows: list[tuple[str, str, str]], *, width: int,
                  color: bool = False) -> None:
    for key, value, label in rows:
        out.write(f"  {key:<28} {value:<{width}} {_annotation(label, color=color)}\n")


def _setting_display(value: str | tuple[str, ...] | Literal[False] | None) -> str:
    """The three states the four stack-dependent settings share."""
    if value is None:
        return "(unset)"
    if value is False:
        return "(none needed)"
    return ", ".join(value) if isinstance(value, tuple) else value


def render_config(eff: EffectiveConfig, out: TextIO, *, state_dir: Path | None,
                  color: bool = False, show_all: bool = False) -> None:
    if state_dir is not None:
        out.write(f"project state: {display_path(state_dir)}\n\n")
    cfg, prov = eff.config, eff.provenance
    configured = set(cfg.providers)
    tier_rows: list[tuple[str, str, str]] = []
    for tier in sorted(cfg.tiers):
        for vendor in sorted(cfg.tiers[tier]):
            if not show_all and configured and vendor not in configured:
                continue
            src = prov.get(("tiers", tier, vendor), FLOOR_SRC)
            tier_rows.append(
                (f"{tier} {vendor}", cfg.tiers[tier][vendor], layer_label(src.file)))
    setting_rows: list[tuple[str, str, str]] = []
    for key, val in (
        ("default-workflow", cfg.default_workflow),
        ("providers", ", ".join(cfg.providers) or "(none)"),
        ("theme", cfg.theme),
        ("editing_mode", cfg.editing_mode),
        ("pythonpath", _setting_display(cfg.pythonpath)),
        ("test_cmd_fast", _setting_display(cfg.test_cmd_fast)),
        ("test_cmd_slow", _setting_display(cfg.test_cmd_slow)),
        ("test_cmd_fast_timeout", "(default 120s)" if cfg.test_cmd_fast_timeout is None
         else f"{cfg.test_cmd_fast_timeout}s"),
        ("test_cmd_slow_timeout", "(default 300s)" if cfg.test_cmd_slow_timeout is None
         else f"{cfg.test_cmd_slow_timeout}s"),
        ("worktree_setup", _setting_display(cfg.worktree_setup)),
    ):
        leaf_src = prov.get((key,))
        label = layer_label(leaf_src.file) if leaf_src is not None else "default"
        setting_rows.append((key, val, label))
    width = max([24, *(len(v) for _, v, _ in (*tier_rows, *setting_rows))])
    out.write("tiers:\n")
    _emit_section(out, tier_rows, width=width, color=color)
    out.write("\nsettings:\n")
    _emit_section(out, setting_rows, width=width, color=color)

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
    rows = [
        (vendor, cfg.tiers[tier][vendor],
         prov.get(("tiers", tier, vendor), FLOOR_SRC).file)
        for vendor in sorted(cfg.tiers[tier])
    ]
    for vendor, value, src_file in rows:
        value_cell = f"{value:<24}"
        if color:
            value_cell = pal.ansi(value_cell, "bold")
        out.write(f"  {vendor:<28} {value_cell} "
                  f"{_annotation(layer_label(src_file), color=color)}\n")
