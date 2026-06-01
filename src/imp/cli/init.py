"""`imp init`: scaffold imp/ in a project.

Spec §8.2: ask for providers + env-var names; write imp/config.yaml,
imp/workflows/default/; append .imp/ to project .gitignore.

Committed config+workflows live under imp/ (tracked by git).
Runtime state (changes, locks, etc.) lives under .imp/ (gitignored wholesale).

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
    "claude_code": {
        "thinking":  "claude-opus-4-7",
        "coding":    "claude-sonnet-4-6",
        "quick":     "claude-haiku-4-5-20251001",
    },
    "anthropic": {
        "thinking":  "claude-opus-4-7",
        "coding":    "claude-sonnet-4-6",
        "quick":     "claude-haiku-4-5-20251001",
    },
    "openai": {
        "thinking": "gpt-5.2",
        "coding":   "gpt-5",
        "quick":    "gpt-5-mini",
    },
}

# Providers driven by a local CLI rather than an HTTP API — no env-var prompt
# at init time, and omitted from the informational `providers:` block.
_PROVIDERS_WITHOUT_ENV: frozenset[str] = frozenset({"claude_code"})

_ROLE_TIERS: dict[str, str | dict[str, str]] = {
    "default":       "coding",
    "spec":          "thinking",
    "spec_review":   "thinking",
    "plan":          "thinking",
    "plan_review":   "thinking",
    "task_lead":     "coding",
    "design":        "thinking",
    "design_review": "thinking",
    "code":          "quick",
    "code_review":   {"default": "coding", "openai": "thinking"},
    "test":          "coding",
    "review":        "thinking",
    "quick":         "quick",
    "fast":          "quick",
    "balanced":      "coding",
    "powerful":      "thinking",
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
            # tier is either a plain tier string (uniform across providers) or a
            # {provider: tier} mapping that MUST carry a 'default' for any
            # enabled provider not named explicitly (e.g. claude_code).
            if isinstance(tier, dict):
                prov_tier = tier[prov] if prov in tier else tier["default"]
            else:
                prov_tier = tier
            model = _PROVIDER_MODELS[prov][prov_tier]
            prefer.append({"provider": prov, "model": model})
        roles[role] = {"prefer": prefer}
    return {
        # TODO: load_config() doesn't yet consume the `providers:` block —
        # the env-var info is informational only; per-role `prefer` entries
        # are what runtime resolution reads.
        "providers": {p: {"env": env_vars[p]} for p in providers if p in env_vars},
        "roles": roles,
    }


def _interactive_provider_picker(available: list[str]) -> list[str]:
    """Inline cursor picker rendered with prompt_toolkit.

    Renders in place (not a full-screen dialog):
        Which providers do you want to enable?
        > [ ] claude_code
          [ ] anthropic
          [ ] openai
        up/down move | space toggle | enter confirm | esc cancel

    Keys: ↑↓ move cursor, space toggles, enter confirms (≥1 required),
    ctrl-c / esc raise KeyboardInterrupt.

    Style names match `imp log`'s palette via prompt_toolkit's ANSI aliases:
    bold for the header, ansicyan for the active row, ansibrightblack
    (= our `gray`/`dim`) for the help line.
    """
    from prompt_toolkit import Application
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import Layout
    from prompt_toolkit.layout.containers import HSplit, Window
    from prompt_toolkit.layout.controls import FormattedTextControl
    from prompt_toolkit.layout.dimension import Dimension

    cursor = 0
    selected: set[int] = set()  # default: none selected

    def _render() -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = [
            ("bold", "Which providers do you want to enable?\n"),
        ]
        for i, name in enumerate(available):
            arrow = "❯ " if i == cursor else "  "  # noqa: RUF001
            box = "[x] " if i in selected else "[ ] "
            style = "ansicyan bold" if i == cursor else ""
            out.append((style, f"{arrow}{box}{name}\n"))
        out.append((
            "ansibrightblack",
            "↑↓ move · space toggle · enter confirm · esc cancel",
        ))
        return out

    kb = KeyBindings()

    @kb.add("up")
    def _up(_event: Any) -> None:
        nonlocal cursor
        cursor = (cursor - 1) % len(available)

    @kb.add("down")
    def _down(_event: Any) -> None:
        nonlocal cursor
        cursor = (cursor + 1) % len(available)

    @kb.add(" ")
    def _space(_event: Any) -> None:
        if cursor in selected:
            selected.remove(cursor)
        else:
            selected.add(cursor)

    @kb.add("enter")
    def _enter(event: Any) -> None:
        if not selected:
            return  # require at least one
        event.app.exit(result=[available[i] for i in sorted(selected)])

    @kb.add("c-c")
    @kb.add("escape", eager=True)
    def _cancel(event: Any) -> None:
        event.app.exit(exception=KeyboardInterrupt)

    control = FormattedTextControl(text=_render, focusable=True, show_cursor=False)
    body = Window(
        content=control,
        height=Dimension(min=len(available) + 2, max=len(available) + 2),
    )
    app: Any = Application(
        layout=Layout(HSplit([body])),
        key_bindings=kb,
        full_screen=False,
        mouse_support=False,
    )
    return list(app.run())


def run_init(
    project_root: Path,
    *,
    input_fn: Callable[[str], str],
    output_fn: Callable[[str], None],
    picker_fn: Callable[[list[str]], list[str]] | None = None,
) -> None:
    imp_dir = project_root / "imp"
    if imp_dir.exists():
        raise FileExistsError(
            f"imp/ already exists at {imp_dir}; refusing to re-init"
        )

    output_fn(f"Setting up imp/ in {project_root}")

    available = list(_PROVIDER_MODELS.keys())
    pick = picker_fn or _interactive_provider_picker
    providers = pick(available)
    if not providers:
        # The interactive picker enforces ≥1; a custom picker_fn might not.
        raise ValueError("at least one provider must be selected")
    output_fn(f"Enabled: {', '.join(providers)}")

    env_vars: dict[str, str] = {}
    for prov in providers:
        if prov in _PROVIDERS_WITHOUT_ENV:
            continue
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

    # Internal state lives under .imp/ (gitignored wholesale). No per-.imp
    # .gitignore — the project .gitignore ignores .imp/ entirely (spec §8).
    project_gi = project_root / ".gitignore"
    add_lines = [".imp/"]
    existing = project_gi.read_text() if project_gi.exists() else ""
    existing_lines = {ln.strip() for ln in existing.splitlines()}
    needs_append = [line for line in add_lines if line.strip() not in existing_lines]
    if needs_append:
        sep = "" if existing.endswith("\n") or not existing else "\n"
        project_gi.write_text(
            existing + sep + "\n# imp\n" + "\n".join(needs_append) + "\n"
        )

    output_fn("")
    output_fn("Created:")
    output_fn("  imp/config.yaml      (committed — env-var NAMES only, no secrets)")
    output_fn("  imp/workflows/default/")
    output_fn("  .gitignore (appended: .imp/)")
    output_fn("")
    output_fn("Try: imp new \"Your first change\"")
