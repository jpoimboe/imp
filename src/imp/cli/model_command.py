"""Validate a /model or --model argument at the point of entry."""
from __future__ import annotations

from typing import Any

from imp.providers.model_arg import ModelArgError, parse_model_arg


def _available_for_change(change: Any) -> list[str]:
    from imp.providers.effective import load_effective_config
    from imp.providers.resolve import available_providers
    config = load_effective_config(
        project_root=change.project_root, control_root=change.control_root,
    ).config
    return available_providers(config)


def validate_model_arg(arg: str, *, change: Any) -> str | None:
    try:
        parsed = parse_model_arg(arg)
    except ModelArgError as exc:
        return str(exc)
    if parsed.provider is None:
        return None
    if parsed.provider not in _available_for_change(change):
        return (f"provider {parsed.provider!r} is not available — "
                f"nothing changed")
    return None
