"""V1 provider registry. Importing this package registers NOTHING: the builtins
land in providers.resolve._PROVIDERS on the first registry read, via
resolve._ensure_builtins(), so `imp --help` and the parse-only path never pay
for them. A dispatching command still loads them through _apply_theme's
effective-config read.

Registration is idempotent — re-running it is a no-op for already-registered
names (resolve.register_provider rebinds, which is harmless in practice, but
tests that need isolation should call resolve.reset_providers_for_tests()).
"""


def register_builtin_providers() -> None:
    """Register the built-in providers (anthropic, openai, claude_code, codex,
    scripted).

    Called by resolve._ensure_builtins() on the first registry read, and directly
    by tests restoring the registry after reset_providers_for_tests. Every import
    is function-local so that importing this package — which any `imp.providers.X`
    import triggers — pulls in no provider module, and with them no httpx (and no
    rich/click/pygments behind httpx's CLI extra).
    """
    from imp.providers import anthropic as _anthropic
    from imp.providers import claude_code as _claude_code
    from imp.providers import codex as _codex
    from imp.providers import openai as _openai
    from imp.providers import scripted as _scripted
    from imp.providers.resolve import register_provider

    register_provider(
        "anthropic", env_check=_anthropic._env_check, factory=_anthropic._factory,
        tier_defaults=_anthropic.TIER_DEFAULTS, env_var=_anthropic.ENV_VAR,
    )
    register_provider(
        "openai", env_check=_openai._env_check, factory=_openai._factory,
        tier_defaults=_openai.TIER_DEFAULTS, env_var=_openai.ENV_VAR,
    )
    register_provider(
        "claude_code", env_check=_claude_code._env_check, factory=_claude_code._factory,
        tier_defaults=_claude_code.TIER_DEFAULTS, env_var=_claude_code.ENV_VAR,
    )
    register_provider(
        "codex", env_check=_codex._env_check, factory=_codex._factory,
        tier_defaults=_codex.TIER_DEFAULTS, env_var=_codex.ENV_VAR,
    )
    register_provider(
        "scripted", env_check=_scripted._env_check, factory=_scripted._factory,
        tier_defaults=_scripted.TIER_DEFAULTS, env_var=_scripted.ENV_VAR,
    )
