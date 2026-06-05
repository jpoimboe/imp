from imp.providers import resolve


def test_codex_registered_after_import() -> None:
    from imp.providers import register_builtin_providers
    resolve.reset_providers_for_tests()
    register_builtin_providers()
    assert "codex" in resolve._PROVIDERS
