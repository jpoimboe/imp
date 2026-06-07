# tests/test_model_rules.py
from __future__ import annotations

from pathlib import Path

import pytest

from imp.engine.errors import LoadError
from imp.engine.model_rules import find_constraint_cycle, parse_role_constraints
from imp.engine.types import RoleConstraint

_P = Path("workflow.yaml")


def test_parse_none_is_empty():
    assert parse_role_constraints(None, _P) == {}


def test_parse_single_constraint():
    raw = {"code_review": {"differ_provider_from": "code"}}
    out = parse_role_constraints(raw, _P)
    assert out["code_review"].differ_provider_from == "code"
    assert out["code_review"].source is not None


def test_parse_skips_line_marker_keys():
    raw = {"__line__": 3, "code_review": {"differ_provider_from": "code", "__line__": 4}}
    out = parse_role_constraints(raw, _P)
    assert set(out) == {"code_review"}


def test_parse_rejects_non_mapping_root():
    with pytest.raises(LoadError, match="must be a mapping"):
        parse_role_constraints(["not", "a", "map"], _P)


def test_parse_rejects_non_string_target():
    with pytest.raises(LoadError, match="must be a role name"):
        parse_role_constraints({"r": {"differ_provider_from": 5}}, _P)


def test_cycle_none_when_acyclic():
    cs = {"code_review": RoleConstraint(differ_provider_from="code")}
    assert find_constraint_cycle(cs) is None


def test_cycle_self_reference():
    cs = {"a": RoleConstraint(differ_provider_from="a")}
    cyc = find_constraint_cycle(cs)
    assert cyc is not None and cyc[0] == "a"


def test_cycle_two_node():
    cs = {
        "a": RoleConstraint(differ_provider_from="b"),
        "b": RoleConstraint(differ_provider_from="a"),
    }
    cyc = find_constraint_cycle(cs)
    assert cyc is not None
    assert set(cyc) == {"a", "b"}
