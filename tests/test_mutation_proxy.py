from __future__ import annotations

from pathlib import Path

import pytest

import imp.remote.daemon.spawn as spawn_mod
from imp.cli.mutation_proxy import run_mutation


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_run_mutation_resume_delegates_to_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    def _fake_resume(control_root: Path, token: str) -> str:
        seen["control_root"] = control_root
        seen["token"] = token
        return "Resuming @questa…"

    monkeypatch.setattr(spawn_mod, "resume_quest", _fake_resume)
    result = run_mutation(tmp_path, "resume", {"token": "questa"})
    assert result == {"returncode": 0, "message": "Resuming @questa…"}
    assert seen == {"control_root": tmp_path, "token": "questa"}


def test_run_mutation_unknown_op_raises() -> None:
    with pytest.raises(ValueError):
        run_mutation(Path("/nope"), "explode", {})
