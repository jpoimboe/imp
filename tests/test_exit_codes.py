from __future__ import annotations

import ast
import inspect
import textwrap
from collections.abc import Callable
from typing import Any

import pytest

from imp.cli import main, session_run
from imp.cli.exit_codes import ExitCode


def test_the_exit_codes_are_exactly_the_contract_six() -> None:
    # The contract table: 0 finished, 1 failure, 2 usage, 3 startup failed,
    # 4 lost connection, 130 user-ended.
    assert {code.name: int(code) for code in ExitCode} == {
        "OK": 0,
        "FAILED": 1,
        "USAGE": 2,
        "STARTUP_FAILED": 3,
        "LOST_CONNECTION": 4,
        "USER_ENDED": 130,
    }


@pytest.mark.parametrize("fn", [
    session_run._closing_block,
    session_run.run_session_client,
    main._print_leave_disposition,
], ids=lambda fn: fn.__name__)
def test_session_endings_return_named_exit_codes(fn: Callable[..., Any]) -> None:
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    literal_returns = [
        node.lineno for node in ast.walk(tree)
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, int)]
    assert literal_returns == []
