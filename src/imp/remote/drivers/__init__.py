"""Driver registry — the single source of truth for built-in remote drivers.

Each entry maps a friendly `name` to the importable `module` imp runs as
`[sys.executable, "-m", <module>]`, plus discovery metadata (one-line
`description`, the `required_env` credentials it reads, and the `default_events`
applied when the user omits an explicit allowlist)."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BuiltinDriver:
    name: str
    module: str
    description: str
    required_env: tuple[str, ...]
    default_events: tuple[str, ...]


REGISTRY: dict[str, BuiltinDriver] = {
    "telegram": BuiltinDriver(
        name="telegram",
        module="imp.remote.drivers.telegram",
        description="Drive an imp run from a Telegram chat (outbound getUpdates; stdlib-only).",
        required_env=("IMP_TELEGRAM_TOKEN", "IMP_TELEGRAM_CHAT_ID"),
        default_events=("all",),
    ),
    "daemon-connector": BuiltinDriver(
        name="daemon-connector",
        module="imp.remote.drivers.daemon_connector",
        description="Bridge a Quest's stdio to the per-repo remote-control daemon socket.",
        required_env=("IMP_REMOTE_CONTROL_SOCK",),
        default_events=("all",),
    ),
}
