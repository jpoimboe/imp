"""config.yaml `vars:` override layer (spec §10, §11, §14): parsed by
parse_config_layer, validated + applied over the workflow's declared defaults at
Change creation."""

from __future__ import annotations

from pathlib import Path

import pytest

from imp.engine.change import Change
from imp.engine.errors import LoadError
from imp.engine.state import State
from imp.engine.types import SourceLoc, VarDecl, Vars
from imp.providers.config import apply_var_overrides, parse_config_layer
from tests.conftest import _git, _seed_control

_SRC = SourceLoc(file=Path("x"), line=1)


def _vars() -> Vars:
    return Vars((
        VarDecl(name="final_review", type="bool", default=True, source=_SRC),
        VarDecl(name="rounds", type="int", default=3, source=_SRC),
        VarDecl(name="label", type="string", default="x", source=_SRC),
    ))


def test_apply_overrides_declared_var() -> None:
    out = apply_var_overrides(_vars(), {"final_review": False})
    assert out == {"final_review": False, "rounds": 3, "label": "x"}


def test_apply_overrides_unknown_var_raises() -> None:
    with pytest.raises(LoadError, match="not declared"):
        apply_var_overrides(_vars(), {"nope": True})


def test_apply_overrides_type_mismatch_raises() -> None:
    # bool is not int: an int var rejects a bool override.
    with pytest.raises(LoadError, match="expects int"):
        apply_var_overrides(_vars(), {"rounds": True})


def test_apply_overrides_no_overrides_is_defaults() -> None:
    assert apply_var_overrides(_vars(), {}) == {"final_review": True, "rounds": 3, "label": "x"}


def test_apply_overrides_string_var() -> None:
    # a string var accepts a string and rejects a non-string
    out = apply_var_overrides(_vars(), {"label": "shipped"})
    assert out["label"] == "shipped"
    with pytest.raises(LoadError, match="expects string"):
        apply_var_overrides(_vars(), {"label": 3})


def _write_config(root: Path, body: str) -> None:
    cfg_text = (
        "providers: [anthropic]\n"
        "tiers:\n"
        "  low: {anthropic: x}\n"
        "  medium: {anthropic: x}\n"
        "  high: {anthropic: x}\n"
        + body
    )
    (root / "imp" / "config.yaml").write_text(cfg_text)


def test_parse_layer_parses_vars_block(tmp_path: Path) -> None:
    (tmp_path / "imp").mkdir()
    _write_config(tmp_path, "vars:\n  ready_to_merge: true\n")
    layer, _src = parse_config_layer(tmp_path / "imp" / "config.yaml")  # type: ignore[misc]
    assert layer["vars"] == {"ready_to_merge": True}


def test_parse_layer_no_vars_block_omits_key(tmp_path: Path) -> None:
    (tmp_path / "imp").mkdir()
    _write_config(tmp_path, "")
    layer, _src = parse_config_layer(tmp_path / "imp" / "config.yaml")  # type: ignore[misc]
    assert "vars" not in layer


def test_parse_layer_vars_must_be_mapping(tmp_path: Path) -> None:
    (tmp_path / "imp").mkdir()
    _write_config(tmp_path, "vars:\n  - nope\n")
    with pytest.raises(LoadError, match="vars:"):
        parse_config_layer(tmp_path / "imp" / "config.yaml")


def test_override_lands_in_state_at_change_creation(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    # Give the workflow a declared var, then override it in config.
    wf = tmp_path / "imp" / "workflows" / "default" / "workflow.yaml"
    wf.write_text(
        'name: t\ndescription: ""\n'
        "vars:\n  final_review: {type: bool, default: true}\n"
        "phases:\n  - name: p\n    steps:\n      - {name: s, file: agents/stub.md}\n"
    )
    _write_config(tmp_path, "vars:\n  final_review: false\n")
    # Task 6 reader cutover: Change.create reads the merged config via
    # load_effective_config, not the legacy imp/config.yaml above. Put the vars
    # override on the local layer (.imp/config.yaml, gitignored control-root) so
    # the reader sees it.
    (tmp_path / ".imp" / "config.yaml").write_text(
        "providers: [anthropic]\n"
        "tiers:\n"
        "  low: {anthropic: x}\n"
        "  medium: {anthropic: x}\n"
        "  high: {anthropic: x}\n"
        "vars:\n  final_review: false\n"
    )
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "vars")

    ch = Change.create(tmp_path, title="X", change_id="x", base_branch="main")
    st = State.load(ch.state_dir)
    assert st.vars["final_review"] is False
