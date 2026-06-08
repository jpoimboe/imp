"""parse_config_layer tolerates absent tiers/providers and returns only the keys
a layer actually sets (spec §7 step 1)."""
from pathlib import Path

import pytest

from imp.engine.errors import LoadError
from imp.providers.config import parse_config_layer


def test_absent_file_returns_none(tmp_path: Path) -> None:
    assert parse_config_layer(tmp_path / "nope.yaml") is None


def test_tiers_less_layer_is_valid(tmp_path: Path) -> None:
    p = tmp_path / "project.yaml"
    p.write_text("workflow: default\nlint_command: make lint\n")
    layer, src = parse_config_layer(p)  # type: ignore[misc]
    assert "tiers" not in layer
    assert "providers" not in layer
    assert layer["lint_command"] == "make lint"
    assert src.file == p


def test_partial_tiers_override_keeps_only_present_leaves(tmp_path: Path) -> None:
    p = tmp_path / "project.yaml"
    p.write_text("tiers:\n  high: {anthropic: opus-4-9}\n")
    layer, _src = parse_config_layer(p)  # type: ignore[misc]
    assert layer["tiers"] == {"high": {"anthropic": "opus-4-9"}}


def test_present_providers_must_be_non_empty_list(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text("providers: []\n")
    with pytest.raises(LoadError):
        parse_config_layer(p)


def test_valueless_providers_treated_as_absent(tmp_path: Path) -> None:
    # `providers:` on its own line parses to key-present/value-None; the layer
    # must treat it as absent (floor fills it), NOT raise a config.yaml message
    # from a project.yaml file (finding M1).
    p = tmp_path / "project.yaml"
    p.write_text("providers:\ntiers:\nworkflow: default\n")
    layer, _src = parse_config_layer(p)  # type: ignore[misc]
    assert "providers" not in layer
    assert "tiers" not in layer


def test_vars_key_normalized_to_present_dict(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text("vars:\n  fast: true\n")
    layer, _src = parse_config_layer(p)  # type: ignore[misc]
    assert layer["vars"] == {"fast": True}
