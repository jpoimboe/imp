from imp.providers.codex import CodexProvider, _env_check, _factory


def test_env_check_false_when_codex_binary_missing(monkeypatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _: None)
    assert _env_check() is False


def test_env_check_true_with_only_the_binary_present(monkeypatch) -> None:
    """Binary-only, like claude_code's: no importable-package half survives, so a
    run must not be gated on any python package being installed."""
    monkeypatch.setattr("shutil.which", lambda _: "/usr/local/bin/codex")
    monkeypatch.setattr("importlib.util.find_spec", lambda _name: None)
    assert _env_check() is True


def test_factory_returns_codex_provider() -> None:
    prov = _factory("gpt-5.5")
    assert isinstance(prov, CodexProvider)
    assert prov.name == "codex"
    assert prov.model == "gpt-5.5"
