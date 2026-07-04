"""`imp init`: scaffold imp/ in a project.

Spec §4: split output. A committed imp/project.yaml (workflow ref + empty
vars — NO bindings, NO theme) and a bootstrapped ~/.config/imp/config.yaml
(providers + theme + editing_mode). On a clone that already carries the
committed imp/project.yaml, init is idempotent: it never overwrites the
project file and only bootstraps the machine's user config if absent.

Committed config+workflows live under imp/ (tracked by git).
Runtime state (changes, locks, etc.) lives under .imp/ (gitignored wholesale).

Tier→model bindings come from the provider-code floor now, so init asks no
model question — it just records the enabled providers + detected theme.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from imp.cli.verify_detect import (
    detect_test_command,
    format_test_cmd_fast_yaml,
    resolve_test_cmd_choice,
)
from imp.engine.sandbox import sandbox_available
from imp.remote.drivers import REGISTRY

_INIT_COMMIT_PATHS: tuple[str, ...] = ("imp/project.yaml", ".gitignore")


class GitSetupDeclined(Exception):
    """The user declined `imp init`'s offer to set up a git repository."""


def _in_git_repo(project_root: Path) -> bool:
    return subprocess.run(
        ["git", "rev-parse", "--git-dir"],
        cwd=project_root, capture_output=True, text=True,
    ).returncode == 0


def _ensure_git_repo(
    project_root: Path, *,
    input_fn: Callable[[str], str],
    output_fn: Callable[[str], None],
) -> None:
    if _in_git_repo(project_root):
        return
    answer = input_fn("Initialize a git repository here? [Y/n] ").strip().lower()
    if answer in ("n", "no"):
        raise GitSetupDeclined(
            "imp needs a git repository — re-run `imp init` and allow git setup."
        )
    subprocess.run(["git", "init"], cwd=project_root, check=True,
                   capture_output=True, text=True)
    output_fn("Initialized a git repository.")


def _commit_init_files(project_root: Path) -> bool:
    """Stage and commit init files by explicit pathspec; return True iff a commit was created."""
    status = subprocess.run(
        ["git", "status", "--porcelain", "--", *_INIT_COMMIT_PATHS],
        cwd=project_root, capture_output=True, text=True, check=True)
    if not status.stdout.strip():
        return False
    subprocess.run(["git", "add", "--", *_INIT_COMMIT_PATHS],
                   cwd=project_root, check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "commit", "-m", "imp: initialize project",
         "--", *_INIT_COMMIT_PATHS],
        cwd=project_root, check=True, capture_output=True, text=True)
    return True


# The order providers are presented in the picker. Bindings (tier→model) are
# owned by the provider-code floor now, so init no longer carries a model table.
_PROVIDER_DISPLAY_ORDER: tuple[str, ...] = (
    "claude_code", "anthropic", "openai", "codex",
)

# Providers driven by a local CLI rather than an HTTP API — no env-var prompt
# at init time, and omitted from the informational env hints. codex
# authenticates via its own ~/.codex/auth.json (codex login), like claude_code.
_PROVIDERS_WITHOUT_ENV: frozenset[str] = frozenset({"claude_code", "codex"})


def _default_detect_theme() -> str:
    """Real terminal-theme detection (dark | light | ansi). Wrapped so a failure
    in the OSC-11 path can never abort init — any error degrades to 'ansi'."""
    try:
        from imp.cli.term_theme import detect_theme
        return detect_theme()
    except Exception:
        return "ansi"


def _build_project_config() -> dict[str, Any]:
    """The committed imp/project.yaml: workflow ref + empty vars. No bindings
    (the provider-code floor supplies tiers); no theme (user-homed)."""
    return {"default-workflow": "imp", "vars": {}}


def _build_user_config(
    providers: list[str], theme: str, editing_mode: str = "emacs",
) -> dict[str, Any]:
    """The bootstrapped ~/.config/imp/config.yaml: providers (detected here),
    theme (detected), editing_mode."""
    return {
        "providers": list(providers),
        "theme": theme,
        "editing_mode": editing_mode,
    }


def _facts_hint() -> str:
    return (
        "\n# Project facts: human-authored values any agent prompt can read via\n"
        "# {{ project.<key> }}. All commented out -- uncomment and edit to expose\n"
        "# facts to your agents. The freeform 'guidance:' below is delivered to\n"
        "# agents automatically by the engine (not via a template var).\n"
        "# facts:\n"
        "#   test_command: make test\n"
        "#   stack: python 3.12, pytest\n"
        "# guidance: |\n"
        "#   Freeform notes for agents (conventions, gotchas).\n"
    )


def _remote_hint() -> str:
    lines = [
        "",
        "# Remote driver: watch and drive a run from a chat surface. Enable ONE",
        "# built-in by name (credentials stay in the environment, never here):",
        "# remote:",
        "#   driver: telegram",
        "# Built-ins and the env vars each needs:",
    ]
    for name in sorted(REGISTRY):
        entry = REGISTRY[name]
        lines.append(f"#   {name} — {entry.description} env: {', '.join(entry.required_env)}")
    return "\n".join(lines) + "\n"


# claude_code (Anthropic) is the recommended single-provider default (review
# architecture spec, M2). Pre-select it in the picker so the common path produces
# a single-provider config; multi-select stays available for codex/openai.
_RECOMMENDED_DEFAULT = "claude_code"


def _initial_selection(available: list[str]) -> set[int]:
    """Indices pre-selected when the picker opens: the recommended default if it
    is offered, else nothing (the picker still requires >=1 before confirm)."""
    if _RECOMMENDED_DEFAULT in available:
        return {available.index(_RECOMMENDED_DEFAULT)}
    return set()


def _picker_rows(
    available: list[str], cursor: int, selected: set[int],
) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = [
        ("bold", "Which providers do you want to enable?\n"),
    ]
    for i, name in enumerate(available):
        arrow = "> " if i == cursor else "  "
        box = "[x] " if i in selected else "[ ] "
        style = "ansicyan bold" if i == cursor else ""
        out.append((style, f"{arrow}{box}{name}\n"))
    out.append((
        "ansibrightblack",
        "↑↓ move · space toggle · enter confirm · esc cancel",
    ))
    return out


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
    selected: set[int] = _initial_selection(available)  # pre-select the recommended default

    def _render() -> list[tuple[str, str]]:
        return _picker_rows(available, cursor, selected)

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
    detect_theme_fn: Callable[[], str] | None = None,
    detect_verify_fn: Callable[[Path], str | None] | None = None,
) -> None:
    from imp.providers.effective import user_config_path

    imp_dir = project_root / "imp"
    project_file = imp_dir / "project.yaml"
    already_initialized = project_file.is_file()

    _ensure_git_repo(project_root, input_fn=input_fn, output_fn=output_fn)

    output_fn(f"Setting up imp in {project_root}")

    available = list(_PROVIDER_DISPLAY_ORDER)
    pick = picker_fn or _interactive_provider_picker
    providers = pick(available)
    if not providers:
        # The interactive picker enforces ≥1; a custom picker_fn might not.
        raise ValueError("at least one provider must be selected")
    output_fn(f"Enabled: {', '.join(providers)}")

    # Confirm the conventional env-var name per HTTP provider (names, never
    # secrets). The floor owns env names now, so there is nothing to *collect* —
    # just confirm, no input round-trip (finding M3). `input_fn` stays in the
    # signature for callers/tests but is no longer used for key names.
    for prov in providers:
        if prov in _PROVIDERS_WITHOUT_ENV:
            continue
        output_fn(f"  {prov} → {prov.upper()}_API_KEY (set this env var)")

    # Detect the terminal theme now (interactive, TTY available) and persist it,
    # so the runtime never has to re-query the terminal. Falls back to 'ansi'.
    detect = detect_theme_fn or _default_detect_theme
    theme = detect()
    if theme != "ansi":
        output_fn(f"Detected a {theme} terminal — colors tuned to match.")

    # --- sandbox posture (spec B.7): probe bwrap, surface if unavailable ---
    if not sandbox_available():
        output_fn("")
        output_fn("WARNING: bubblewrap (bwrap) not found — imp can't OS-sandbox the agent's")
        output_fn("   shell. Install it for a real filesystem boundary:")
        output_fn("     Fedora:  sudo dnf install bubblewrap")
        output_fn("     Debian:  sudo apt install bubblewrap")
        output_fn("   Until then, set `sandbox.allow_unsandboxed: true` in your")
        output_fn(f"   user config ({user_config_path()}) to run without a boundary,")
        output_fn("   or runs will hard-fail. (You can also re-run init after installing.)")
        output_fn("")

    # --- committed project file (idempotent: never overwrite an existing one) ---
    if not already_initialized:
        imp_dir.mkdir(parents=True, exist_ok=True)
        body = yaml.safe_dump(
            _build_project_config(), sort_keys=False, default_flow_style=False)
        detect_cmd = detect_verify_fn or detect_test_command
        suggested = detect_cmd(project_root)
        test_block = ""
        if suggested is not None:
            chosen = resolve_test_cmd_choice(suggested, input_fn, output_fn)
            test_block = (
                "\n# Fast test gate: lint + typecheck + fast tests, run after each\n"
                "# implement loop to catch regressions. Empty string opts out.\n"
                "# The slow gate (test_cmd_slow) runs the full suite once per Change\n"
                "# and falls back to test_cmd_fast when unset. Timeouts default to\n"
                "# 120s (fast) / 300s (slow) — override with test_cmd_fast_timeout /\n"
                "# test_cmd_slow_timeout.\n"
                + format_test_cmd_fast_yaml(chosen)
            )
        project_file.write_text(body + test_block + _facts_hint())

    # --- bootstrap user config (only if the machine has none) ---
    user_path = user_config_path()
    wrote_user_config = not user_path.exists()
    if wrote_user_config:
        user_path.parent.mkdir(parents=True, exist_ok=True)
        user_path.write_text(
            yaml.safe_dump(
                _build_user_config(providers, theme),
                sort_keys=False, default_flow_style=False)
            + _remote_hint())

    # --- ensure .imp/ is gitignored (control-root state lives there) ---
    project_gi = project_root / ".gitignore"
    existing = project_gi.read_text() if project_gi.exists() else ""
    existing_lines = {ln.strip() for ln in existing.splitlines()}
    if ".imp/" not in existing_lines:
        sep = "" if existing.endswith("\n") or not existing else "\n"
        project_gi.write_text(existing + sep + "\n# imp\n.imp/\n")

    committed = _commit_init_files(project_root)

    output_fn("")
    if already_initialized:
        output_fn("Project already initialized (imp/project.yaml is committed).")
        if wrote_user_config:
            output_fn("Bootstrapped your machine config (providers, theme).")
        else:
            output_fn("Your machine config is already set up — nothing to do.")
    else:
        output_fn("Created:")
        output_fn("  imp/project.yaml")
    output_fn(f"  {user_path}  (your machine: providers, theme)")
    if committed:
        output_fn("Committed imp/project.yaml.")
    output_fn("")
    output_fn("Try: imp new \"Add a dark-mode toggle\"")
