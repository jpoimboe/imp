"""V1 provider registry. Importing this package eagerly registers builtin
providers in providers.resolve._PROVIDERS so resolve_role() can find them.

Registration is idempotent — re-importing this module is a no-op for
already-registered names (resolve.register_provider rebinds, which is harmless
in practice but tests that need isolation should call
resolve.reset_providers_for_tests()).
"""

from imp.providers import anthropic as _anthropic
from imp.providers import claude_code as _claude_code  # Task 24 lands this
from imp.providers import openai as _openai
from imp.providers.resolve import register_provider


def register_builtin_providers() -> None:
    """Register the built-in providers (anthropic, openai, claude_code).

    Run at import time; also called by tests that rely on the global registry
    to restore it after another test cleared it via reset_providers_for_tests.
    """
    register_provider("anthropic",   env_check=_anthropic._env_check,  factory=_anthropic._factory)
    register_provider("openai",      env_check=_openai._env_check,     factory=_openai._factory)
    register_provider(
        "claude_code",
        env_check=_claude_code._env_check,
        factory=_claude_code._factory,
    )


register_builtin_providers()
