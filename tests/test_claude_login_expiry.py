"""The ~30-day /login window: reading it, and the three-day warning it drives."""
from __future__ import annotations

import pytest

from imp.providers import claude_token_refresh as ctr

NOW = 1_000_000_000_000
DAY = 24 * 60 * 60 * 1000
RENEW = "run `claude` then `/login` to renew"


@pytest.mark.parametrize(("remaining_ms", "expected"), [
    (10 * DAY, None),
    (4 * DAY, None),
    (3 * DAY + 1, None),                                            # just outside the window
    (3 * DAY, f"Claude login expires in 3 days — {RENEW}"),         # exactly on it
    (2 * DAY + DAY // 2, f"Claude login expires in 2 days — {RENEW}"),   # floors, not rounds
    (2 * DAY, f"Claude login expires in 2 days — {RENEW}"),
    (DAY, f"Claude login expires in 1 day — {RENEW}"),              # singular noun at 24h
    (DAY - 1, f"Claude login expires in under a day — {RENEW}"),
    (DAY // 2, f"Claude login expires in under a day — {RENEW}"),
    (1, f"Claude login expires in under a day — {RENEW}"),
    (0, f"Claude login has expired — {RENEW}"),
    (-DAY, f"Claude login has expired — {RENEW}"),
])
def test_login_expiry_warning_boundaries(
        remaining_ms: int, expected: str | None) -> None:
    assert ctr.login_expiry_warning(NOW, NOW + remaining_ms) == expected


def test_no_known_expiry_is_silent() -> None:
    assert ctr.login_expiry_warning(NOW, None) is None
