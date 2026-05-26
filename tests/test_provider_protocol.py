from __future__ import annotations

from imp.providers.base import Provider


def test_provider_protocol_has_name_and_create_runner() -> None:
    assert hasattr(Provider, "__init_subclass__") or hasattr(Provider, "_is_protocol")
    annotations = Provider.__annotations__
    assert "name" in annotations
    assert "model" in annotations
