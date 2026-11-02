from __future__ import annotations

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
