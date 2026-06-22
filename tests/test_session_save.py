from __future__ import annotations

from pathlib import Path

import pytest

from imp.engine.change import Change
from imp.engine.state import State
from tests.conftest import _seed_control


def test_save_change_non_resident_marks_status_saved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import imp.remote.session.save as savemod

    _seed_control(tmp_path)
    change = Change.create_draft(tmp_path, title="x", change_id="feat", base_branch="main")
    monkeypatch.setattr(savemod, "session_is_resident", lambda *_a: False)
    savemod.save_change(tmp_path, "feat")
    assert State.load(change.state_dir).status == "saved"


def test_save_change_non_resident_ignores_materialized_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import imp.remote.session.save as savemod

    _seed_control(tmp_path)
    change = Change.create(tmp_path, title="x", change_id="feat2", base_branch="main")
    assert change.materialized  # confirm it starts materialized
    monkeypatch.setattr(savemod, "session_is_resident", lambda *_a: False)
    savemod.save_change(tmp_path, "feat2")
    # Status must NOT be flipped to "saved"
    assert State.load(change.state_dir).status != "saved"


def test_save_change_resident_delivers_the_control(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import imp.remote.session.save as savemod

    delivered: list[str] = []
    monkeypatch.setattr(savemod, "session_is_resident", lambda *_a: True)
    monkeypatch.setattr(
        savemod, "_deliver_save_resident",
        lambda _c, cid: delivered.append(cid))
    savemod.save_change(tmp_path, "feat")
    assert delivered == ["feat"]
