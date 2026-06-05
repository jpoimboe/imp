import importlib.util
import sys
from types import ModuleType

from imp.providers.codex import CodexProvider, _env_check, _factory


def test_env_check_false_when_codex_binary_missing(monkeypatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _: None)
    assert _env_check() is False


def test_env_check_false_when_sdk_not_importable(monkeypatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _: "/usr/local/bin/codex")
    monkeypatch.setattr(
        "importlib.util.find_spec",
        lambda name: None if name == "openai_codex_sdk" else importlib.util.find_spec(name),
    )
    assert _env_check() is False


def test_env_check_true_when_both_present(monkeypatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _: "/usr/local/bin/codex")
    stub = ModuleType("openai_codex_sdk")
    monkeypatch.setitem(sys.modules, "openai_codex_sdk", stub)
    assert _env_check() is True


def test_factory_returns_codex_provider() -> None:
    prov = _factory("gpt-5.5")
    assert isinstance(prov, CodexProvider)
    assert prov.name == "codex"
    assert prov.model == "gpt-5.5"
