"""Single definition site for unattended-run workflow var overrides.

`spec_confirm` defaults TRUE in the shipped workflow, so every harness that runs
the full workflow without a human at the keyboard must disable the confirm gate.
Centralizing it here means new harnesses inherit the override instead of
re-discovering it. Feature-2 escalations are deliberately NOT suppressed here —
a headless escalation is an accepted AWAITING_USER pause.
"""
from __future__ import annotations


def unattended_vars(**extra: bool) -> dict[str, bool]:
    """The workflow var overrides every unattended harness should apply.

    Pins `spec_confirm: false`; callers add harness-specific extras."""
    return {"spec_confirm": False, **extra}


def render_vars_yaml(vars_: dict[str, bool]) -> str:
    """Render a `vars:` config block (lowercase JSON-style bools for YAML)."""
    lines = "".join(
        f"  {k}: {'true' if v else 'false'}\n" for k, v in vars_.items()
    )
    return f"vars:\n{lines}"
