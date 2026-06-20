import re

from imp.engine.change_id import new_change_id


def test_new_change_id_is_six_refsafe_chars() -> None:
    cid = new_change_id()
    assert re.fullmatch(r"[a-z0-9]{6}", cid)


def test_new_change_id_varies_across_calls() -> None:
    # Random draw: 50 ids must not collapse to a single value.
    assert len({new_change_id() for _ in range(50)}) > 1
