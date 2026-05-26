"""`imp init`: scaffold .imp/ in a project.

Spec §8.2: ask for providers + env-var names; write config.yaml,
workflows/default/, .gitignore; append to project .gitignore.

The default role taxonomy (spec §8.2) is auto-populated based on enabled
providers — no model selection question, just sensible defaults.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from imp.assets import default_workflow_dir

# Per spec §8.2 role taxonomy. Order: thinking-heavy → coding → quick.
# Models per provider chosen for current best-in-class as of V0.
_PROVIDER_MODELS: dict[str, dict[str, str]] = {
    "anthropic": {
        "thinking":  "claude-opus-4-7",
        "coding":    "claude-sonnet-4-6",
        "quick":     "claude-haiku-4-5-20251001",
    },
    "openai": {
        "thinking": "gpt-5",
        "coding":   "gpt-5",
        "quick":    "gpt-5-mini",
    },
}

_ROLE_TIERS = {
    "default":       "coding",
    "spec":          "thinking",
    "spec_review":   "thinking",
    "design":        "thinking",
    "design_review": "thinking",
    "code":          "coding",
    "code_review":   "thinking",
    "test":          "coding",
    "review":        "thinking",
    "quick":         "quick",
}


def _build_role_map(
    providers: list[str], env_vars: dict[str, str],
) -> dict[str, Any]:
    """Build the `roles:` block — each role lists providers in order;
    first available env-var wins at runtime."""
    roles: dict[str, Any] = {}
    for role, tier in _ROLE_TIERS.items():
        prefer = []
        for prov in providers:
            model = _PROVIDER_MODELS[prov][tier]
            prefer.append({"provider": prov, "model": model})
        roles[role] = {"prefer": prefer}
    return {
        # TODO: load_config() doesn't yet consume the `providers:` block —
        # the env-var info is informational only; per-role `prefer` entries
        # are what runtime resolution reads.
        "providers": {p: {"env": env_vars[p]} for p in providers},
        "roles": roles,
    }


def _split_providers(answer: str) -> tuple[list[str], list[str]]:
    """Accept 'anthropic', 'anthropic openai', 'anthropic, openai' etc.
    Returns (valid, unknown) — unknown tokens are surfaced to caller for warning."""
    parts = [p.strip().lower() for p in answer.replace(",", " ").split() if p.strip()]
    valid = [p for p in parts if p in _PROVIDER_MODELS]
    unknown = [p for p in parts if p not in _PROVIDER_MODELS]
    return valid, unknown


def run_init(
    project_root: Path,
    *,
    input_fn: Callable[[str], str],
    output_fn: Callable[[str], None],
) -> None:
    imp_dir = project_root / ".imp"
    if imp_dir.exists():
        raise FileExistsError(
            f".imp/ already exists at {imp_dir}; refusing to re-init"
        )

    output_fn(f"Setting up .imp/ in {project_root}")

    while True:
        ans = input_fn(
            "Which providers do you want to enable? "
            "(space-separated; choices: anthropic openai): "
        )
        providers, unknown = _split_providers(ans)
        for bad in unknown:
            output_fn(f"  (unknown provider ignored: {bad!r})")
        if providers:
            break
        output_fn("  (please pick at least one of: anthropic, openai)")

    env_vars: dict[str, str] = {}
    for prov in providers:
        default_env = f"{prov.upper()}_API_KEY"
        ans = input_fn(f"For {prov!r}: API key env var [{default_env}]: ")
        env_vars[prov] = (ans.strip() or default_env)

    imp_dir.mkdir()
    (imp_dir / "workflows").mkdir()
    shutil.copytree(default_workflow_dir(), imp_dir / "workflows" / "default")

    cfg = _build_role_map(providers, env_vars)
    (imp_dir / "config.yaml").write_text(
        yaml.safe_dump(cfg, sort_keys=False, default_flow_style=False)
    )

    (imp_dir / ".gitignore").write_text(
        "# imp local state — keep secrets and per-Change data out of git\n"
        "config.yaml\n"
        "changes/\n"
        "lock\n"
    )

    project_gi = project_root / ".gitignore"
    add_lines = [".imp/config.yaml", ".imp/changes/", ".imp/lock"]
    existing = project_gi.read_text() if project_gi.exists() else ""
    needs_append = [l for l in add_lines if l not in existing]
    if needs_append:
        sep = "" if existing.endswith("\n") or not existing else "\n"
        project_gi.write_text(
            existing + sep + "\n# imp\n" + "\n".join(needs_append) + "\n"
        )

    output_fn("")
    output_fn("Created:")
    output_fn("  .imp/config.yaml      (gitignored)")
    output_fn("  .imp/workflows/default/")
    output_fn("  .imp/.gitignore")
    output_fn("  .gitignore (appended)")
    output_fn("")
    output_fn("Try: imp new \"Your first change\"")
