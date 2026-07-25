"""parse_config_layer tolerates absent tiers/providers and returns only the keys
a layer actually sets (spec §7 step 1)."""
from pathlib import Path

import pytest

from imp.engine.errors import LoadError
from imp.engine.types import SourceLoc
from imp.providers.config import parse_config_layer
from imp.providers.effective import load_effective_config, merge_layers


def test_absent_file_returns_none(tmp_path: Path) -> None:
    assert parse_config_layer(tmp_path / "nope.yaml") is None


def test_tiers_less_layer_is_valid(tmp_path: Path) -> None:
    p = tmp_path / "project.yaml"
    p.write_text("default-workflow: imp\ntest_cmd_fast: make check\n")
    layer, src = parse_config_layer(p)  # type: ignore[misc]
    assert "tiers" not in layer
    assert "providers" not in layer
    assert layer["test_cmd_fast"] == "make check"
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
    p.write_text("providers:\ntiers:\ndefault-workflow: imp\n")
    layer, _src = parse_config_layer(p)  # type: ignore[misc]
    assert "providers" not in layer
    assert "tiers" not in layer


def test_vars_key_normalized_to_present_dict(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text("vars:\n  fast: true\n")
    layer, _src = parse_config_layer(p)  # type: ignore[misc]
    assert layer["vars"] == {"fast": True}


# --- sandbox block tests ---


def test_parse_sandbox_block(tmp_path: Path) -> None:
    p = tmp_path / "project.yaml"
    p.write_text(
        "default-workflow: imp\n"
        "sandbox:\n"
        "  network: false\n"
        "  secret_paths: ['.mykeys']\n"
    )
    out, _ = parse_config_layer(p)  # type: ignore[misc]
    assert out["sandbox"]["network"] is False
    assert out["sandbox"]["secret_paths"] == [".mykeys"]


def test_sandbox_merges_one_level(tmp_path: Path) -> None:
    # user layer sets allow_unsandboxed; project layer sets network — both survive.
    src = SourceLoc(file=tmp_path / "x", line=1)
    merged, _ = merge_layers([
        ({"sandbox": {"network": False}}, src),
        ({"sandbox": {"allow_unsandboxed": True}}, src),
    ])
    assert merged["sandbox"] == {"network": False, "allow_unsandboxed": True}


def test_config_exposes_sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IMP_HOME", str(tmp_path / "imphome"))
    # With no config files, sandbox defaults to an empty mapping (code defaults
    # apply in SandboxConfig downstream).
    eff = load_effective_config(project_root=None, control_root=None)
    assert isinstance(eff.config.sandbox, dict)


def test_sandbox_unknown_key_rejected(tmp_path: Path) -> None:
    p = tmp_path / "c.yaml"
    p.write_text("default-workflow: imp\nsandbox:\n  bogus_key: 1\n")
    with pytest.raises(LoadError, match="not a known sandbox setting"):
        parse_config_layer(p)


def test_sandbox_bad_bool_rejected(tmp_path: Path) -> None:
    p = tmp_path / "c.yaml"
    p.write_text("default-workflow: imp\nsandbox:\n  enabled: yes-please\n")
    with pytest.raises(LoadError, match="must be a boolean"):
        parse_config_layer(p)


def test_sandbox_bad_list_rejected(tmp_path: Path) -> None:
    p = tmp_path / "c.yaml"
    p.write_text("default-workflow: imp\nsandbox:\n  secret_paths: /etc/shadow\n")
    with pytest.raises(LoadError, match="must be a list of strings"):
        parse_config_layer(p)


def test_sandbox_non_mapping_rejected(tmp_path: Path) -> None:
    p = tmp_path / "c.yaml"
    p.write_text("default-workflow: imp\nsandbox: flat-string\n")
    with pytest.raises(LoadError, match="must be a mapping"):
        parse_config_layer(p)


# --- context block tests ---


def test_parse_context_block(tmp_path: Path) -> None:
    p = tmp_path / "project.yaml"
    p.write_text(
        "default-workflow: imp\n"
        "context:\n"
        "  checkpoint_threshold: 0.8\n"
        "  max_reincarnations: 3\n"
    )
    out, _ = parse_config_layer(p)  # type: ignore[misc]
    assert out["context"] == {"checkpoint_threshold": 0.8, "max_reincarnations": 3}


def test_context_absolute_threshold_allowed(tmp_path: Path) -> None:
    p = tmp_path / "c.yaml"
    p.write_text("default-workflow: imp\ncontext:\n  checkpoint_threshold: 150000\n")
    out, _ = parse_config_layer(p)  # type: ignore[misc]
    assert out["context"]["checkpoint_threshold"] == 150000


def test_context_unknown_key_rejected(tmp_path: Path) -> None:
    p = tmp_path / "c.yaml"
    p.write_text("default-workflow: imp\ncontext:\n  bogus: 1\n")
    with pytest.raises(LoadError, match="not a known context setting"):
        parse_config_layer(p)


def test_context_bad_threshold_rejected(tmp_path: Path) -> None:
    p = tmp_path / "c.yaml"
    p.write_text("default-workflow: imp\ncontext:\n  checkpoint_threshold: 0\n")
    with pytest.raises(LoadError, match="checkpoint_threshold"):
        parse_config_layer(p)


def test_context_bad_max_reincarnations_rejected(tmp_path: Path) -> None:
    p = tmp_path / "c.yaml"
    p.write_text("default-workflow: imp\ncontext:\n  max_reincarnations: 1.5\n")
    with pytest.raises(LoadError, match="max_reincarnations"):
        parse_config_layer(p)


def test_context_non_mapping_rejected(tmp_path: Path) -> None:
    p = tmp_path / "c.yaml"
    p.write_text("default-workflow: imp\ncontext: flat-string\n")
    with pytest.raises(LoadError, match="'context:' must be a mapping"):
        parse_config_layer(p)


def test_context_checkpoint_threshold_bool_rejected(tmp_path: Path) -> None:
    p = tmp_path / "c.yaml"
    p.write_text("default-workflow: imp\ncontext:\n  checkpoint_threshold: true\n")
    with pytest.raises(LoadError, match="checkpoint_threshold"):
        parse_config_layer(p)


def test_context_max_reincarnations_bool_rejected(tmp_path: Path) -> None:
    p = tmp_path / "c.yaml"
    p.write_text("default-workflow: imp\ncontext:\n  max_reincarnations: true\n")
    with pytest.raises(LoadError, match="max_reincarnations"):
        parse_config_layer(p)


def test_context_checkpoint_threshold_negative_rejected(tmp_path: Path) -> None:
    p = tmp_path / "c.yaml"
    p.write_text("default-workflow: imp\ncontext:\n  checkpoint_threshold: -1\n")
    with pytest.raises(LoadError, match="checkpoint_threshold"):
        parse_config_layer(p)


def test_context_max_reincarnations_negative_rejected(tmp_path: Path) -> None:
    p = tmp_path / "c.yaml"
    p.write_text("default-workflow: imp\ncontext:\n  max_reincarnations: -1\n")
    with pytest.raises(LoadError, match="max_reincarnations"):
        parse_config_layer(p)


def test_context_max_reincarnations_zero_accepted(tmp_path: Path) -> None:
    p = tmp_path / "c.yaml"
    p.write_text("default-workflow: imp\ncontext:\n  max_reincarnations: 0\n")
    out, _ = parse_config_layer(p)  # type: ignore[misc]
    assert out["context"] == {"max_reincarnations": 0}


# --- YAML 1.2 read semantics (ruamel safe loader) ---


def test_layer_numeric_forms_read_as_yaml_1_2(tmp_path: Path) -> None:
    # YAML 1.1 read `0777` as octal 511; 1.2 reads it as the decimal 777.
    p = tmp_path / "config.yaml"
    p.write_text("test_cmd_fast_timeout: 0777\n")
    layer, _src = parse_config_layer(p)  # type: ignore[misc]
    assert layer["test_cmd_fast_timeout"] == 777


def test_malformed_layer_raises_load_error_naming_the_file(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text("providers: [claude_code\n")
    with pytest.raises(LoadError) as exc:
        parse_config_layer(p)
    assert "invalid YAML in config.yaml" in str(exc.value)


def test_duplicate_key_raises_load_error(tmp_path: Path) -> None:
    # PyYAML kept the last value silently; ruamel surfaces the repeat.
    p = tmp_path / "config.yaml"
    p.write_text("theme: dark\ntheme: light\n")
    with pytest.raises(LoadError) as exc:
        parse_config_layer(p)
    msg = str(exc.value)
    assert "invalid YAML in config.yaml" in msg
    assert "duplicate key" in msg


# --- pythonpath tests ---


def test_pythonpath_list_parsed(tmp_path: Path) -> None:
    p = tmp_path / "project.yaml"
    p.write_text("pythonpath: [src, .]\n")
    out, _ = parse_config_layer(p)  # type: ignore[misc]
    assert out["pythonpath"] == ["src", "."]


def test_pythonpath_absent_key_omitted(tmp_path: Path) -> None:
    p = tmp_path / "project.yaml"
    p.write_text("default-workflow: imp\n")
    out, _ = parse_config_layer(p)  # type: ignore[misc]
    assert "pythonpath" not in out


def test_valueless_pythonpath_treated_as_absent(tmp_path: Path) -> None:
    # A bare `pythonpath:` (YAML null) => feature off, following the
    # `.get(...) is not None` guard idiom (not the `key in raw` idiom).
    p = tmp_path / "project.yaml"
    p.write_text("pythonpath:\n")
    out, _ = parse_config_layer(p)  # type: ignore[misc]
    assert "pythonpath" not in out


def test_pythonpath_non_list_raises(tmp_path: Path) -> None:
    p = tmp_path / "project.yaml"
    p.write_text("pythonpath: src\n")
    with pytest.raises(LoadError):
        parse_config_layer(p)


def test_pythonpath_non_string_entry_raises(tmp_path: Path) -> None:
    p = tmp_path / "project.yaml"
    p.write_text("pythonpath: [src, 3]\n")
    with pytest.raises(LoadError):
        parse_config_layer(p)


def test_test_cmd_fast_in_tiers_less_layer(tmp_path: Path) -> None:
    p = tmp_path / "project.yaml"
    p.write_text("default-workflow: imp\ntest_cmd_fast: make check\n")
    layer, src = parse_config_layer(p)  # type: ignore[misc]
    assert layer["test_cmd_fast"] == "make check"
    assert src.file == p


def test_context_merges_one_level(tmp_path: Path) -> None:
    # user layer sets max_reincarnations; project sets checkpoint_threshold — both survive.
    src = SourceLoc(file=tmp_path / "x", line=1)
    merged, _ = merge_layers([
        ({"context": {"checkpoint_threshold": 0.75}}, src),
        ({"context": {"max_reincarnations": 4}}, src),
    ])
    assert merged["context"] == {"checkpoint_threshold": 0.75, "max_reincarnations": 4}


def test_config_exposes_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IMP_HOME", str(tmp_path / "imphome"))
    eff = load_effective_config(project_root=None, control_root=None)
    assert isinstance(eff.config.context, dict)
    # With no config files, accessors fall back to documented defaults.
    assert eff.config.context_checkpoint_threshold() == 0.8
    assert eff.config.context_max_reincarnations() == 2
