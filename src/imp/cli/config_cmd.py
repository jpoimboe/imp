"""Read-only `imp config` provenance resolver (spec §5). Never mutates."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TextIO

from imp.cli import palette as pal
from imp.providers.effective import FLOOR_SRC, EffectiveConfig
from imp.providers.resolve import iter_provider_defaults
from imp.remote.drivers import REGISTRY


def _elide(src_file: Path) -> str:
    parts = src_file.parts
    if len(parts) <= 2:
        return str(src_file)
    return "…/" + "/".join(parts[-2:])


def _label(src_file: Path) -> str:
    """Human label for a leaf's source file (the right-hand side of `←`)."""
    if src_file == FLOOR_SRC.file:
        return "provider default"
    return _elide(src_file)


def _annotation(label: str, *, color: bool) -> str:
    """The `← <source>` provenance annotation, dimmed when color is on."""
    text = f"← {label}"
    return pal.ansi(text, "dim") if color else text


def _emit_section(out: TextIO, rows: list[tuple[str, str, str]], *, color: bool = False) -> None:
    """Emit rows with the value column sized to the section's widest value so arrows align."""
    width = max([24, *(len(value) for _, value, _ in rows)])
    for key, value, label in rows:
        out.write(f"  {key:<28} {value:<{width}} {_annotation(label, color=color)}\n")


def render_config(eff: EffectiveConfig, out: TextIO, *, color: bool = False,
                  show_all: bool = False) -> None:
    cfg, prov = eff.config, eff.provenance
    out.write("tiers:\n")
    configured = set(cfg.providers)
    tier_rows: list[tuple[str, str, str]] = []
    for tier in sorted(cfg.tiers):
        for vendor in sorted(cfg.tiers[tier]):
            if not show_all and configured and vendor not in configured:
                continue
            src = prov.get(("tiers", tier, vendor), FLOOR_SRC)
            tier_rows.append(
                (f"{tier} {vendor}", cfg.tiers[tier][vendor], _label(src.file)))
    _emit_section(out, tier_rows, color=color)
    out.write("\nsettings:\n")
    setting_rows: list[tuple[str, str, str]] = []
    for key, val in (
        ("providers", ", ".join(cfg.providers) or "(none)"),
        ("theme", cfg.theme),
        ("editing_mode", cfg.editing_mode),
        ("pythonpath", ", ".join(cfg.pythonpath) or "(unset)"),
        ("test_cmd_fast", "(disabled)" if cfg.test_cmd_fast == ""
         else (cfg.test_cmd_fast or "(unset)")),
        ("test_cmd_slow", "(disabled)" if cfg.test_cmd_slow == ""
         else (cfg.test_cmd_slow or "(unset)")),
        ("test_cmd_fast_timeout", "(default 120s)" if cfg.test_cmd_fast_timeout is None
         else f"{cfg.test_cmd_fast_timeout}s"),
        ("test_cmd_slow_timeout", "(default 300s)" if cfg.test_cmd_slow_timeout is None
         else f"{cfg.test_cmd_slow_timeout}s"),
    ):
        leaf_src = prov.get((key,))
        label = _label(leaf_src.file) if leaf_src is not None else "default"
        setting_rows.append((key, val, label))
    _emit_section(out, setting_rows, color=color)

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
        annotation = f"← {_label(src_file)}"
        if color:
            annotation = pal.ansi(annotation, "dim")
        out.write(f"  {vendor:<28} {value_cell} {annotation}\n")
