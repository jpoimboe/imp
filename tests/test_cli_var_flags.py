"""`--var NAME=VALUE` parsed against the workflow's own declarations."""

from __future__ import annotations

from pathlib import Path

import pytest

from imp.cli.var_flags import VarFlagError, resolve_var_flags
from tests.conftest import _seed_control, _seed_control_with_vars


def test_flags_are_coerced_to_the_declared_types(tmp_path: Path) -> None:
    _seed_control_with_vars(tmp_path)
    assert resolve_var_flags(
        tmp_path, workflow=None,
        raw=["jurisdiction=us", "retries=5", "strict=true"],
    ) == {"jurisdiction": "us", "retries": 5, "strict": True}


def test_false_coerces_to_a_bool_not_a_string(tmp_path: Path) -> None:
    _seed_control_with_vars(tmp_path)
    resolved = resolve_var_flags(tmp_path, workflow=None, raw=["strict=false"])
    assert resolved == {"strict": False}
    assert isinstance(resolved["strict"], bool)


def test_bool_coercion_is_case_insensitive(tmp_path: Path) -> None:
    _seed_control_with_vars(tmp_path)
    assert resolve_var_flags(tmp_path, workflow=None, raw=["strict=True"]) == {"strict": True}
    assert resolve_var_flags(tmp_path, workflow=None, raw=["strict=FALSE"]) == {"strict": False}


def test_a_string_value_may_contain_equals_signs(tmp_path: Path) -> None:
    _seed_control_with_vars(tmp_path)
    assert resolve_var_flags(
        tmp_path, workflow=None, raw=["jurisdiction=a=b"]) == {"jurisdiction": "a=b"}


def test_a_repeated_name_takes_the_last_value(tmp_path: Path) -> None:
    _seed_control_with_vars(tmp_path)
    assert resolve_var_flags(
        tmp_path, workflow=None, raw=["retries=1", "retries=9"]) == {"retries": 9}


def test_no_flags_loads_no_workflow_at_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Skew tolerance: a caller that supplies no overrides must not pay a load.
    _seed_control_with_vars(tmp_path)

    def _boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("workflow loaded with no --var supplied")

    monkeypatch.setattr("imp.engine.loader.load_workflow", _boom)
    assert resolve_var_flags(tmp_path, workflow=None, raw=None) == {}
    assert resolve_var_flags(tmp_path, workflow=None, raw=[]) == {}


def test_a_malformed_argument_is_rejected_before_the_workflow_loads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Shape error must fire before workflow load (skew tolerance).
    _seed_control_with_vars(tmp_path)

    def _boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("workflow loaded before the NAME=VALUE check")

    monkeypatch.setattr("imp.engine.loader.load_workflow", _boom)
    with pytest.raises(VarFlagError) as excinfo:
        resolve_var_flags(tmp_path, workflow=None, raw=["retries=1", "jurisdiction"])
    assert str(excinfo.value) == "--var expects NAME=VALUE, got 'jurisdiction'"


@pytest.mark.parametrize(("raw", "message"), [
    (["jurisdiction"], "--var expects NAME=VALUE, got 'jurisdiction'"),
    (["strict=yes"], "variable 'strict' expects bool, got 'yes'; use true or false"),
    (["retries=many"], "variable 'retries' expects int, got 'many'"),
    (["foo=1"], "unknown variable 'foo' for workflow 'imp'; "
                "declared variables: jurisdiction, retries, strict"),
])
def test_rejections_carry_the_verbatim_cli_message_body(
    tmp_path: Path, raw: list[str], message: str,
) -> None:
    _seed_control_with_vars(tmp_path)
    with pytest.raises(VarFlagError) as excinfo:
        resolve_var_flags(tmp_path, workflow=None, raw=raw)
    assert str(excinfo.value) == message


def test_unknown_name_for_a_workflow_that_declares_nothing(tmp_path: Path) -> None:
    _seed_control(tmp_path)          # the plain seed workflow declares no vars
    with pytest.raises(VarFlagError) as excinfo:
        resolve_var_flags(tmp_path, workflow=None, raw=["foo=1"])
    assert str(excinfo.value) == (
        "unknown variable 'foo' for workflow 'imp'; it declares no variables")
