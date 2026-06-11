from datetime import UTC, datetime

import pytest

from imp.cli.change_row import relative_age, resume_token

_NOW = datetime(2026, 6, 11, 12, 0, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "then_iso, expected",
    [
        ("2026-06-11T11:59:30+00:00", "30s"),   # 30 seconds ago
        ("2026-06-11T11:45:00+00:00", "15m"),   # 15 minutes ago
        ("2026-06-11T06:00:00+00:00", "6h"),    # 6 hours ago
        ("2026-06-08T12:00:00+00:00", "3d"),    # 3 days ago
        ("2026-06-11T13:00:00+00:00", "0s"),    # future clamps to 0s
        (None, "-"),                            # no timestamp
        ("", "-"),                              # empty
        ("not-a-date", "-"),                    # unparseable
    ],
)
def test_relative_age(then_iso, expected):
    assert relative_age(then_iso, now=_NOW) == expected


@pytest.mark.parametrize(
    "change_id, expected",
    [
        ("2026-06-11-yaitd7", "yaitd7"),                 # modern id → 6-char suffix
        ("2026-06-11-add-version-flag",
         "2026-06-11-add-version-flag"),                 # legacy descriptive → full id
        ("legacy-no-date", "legacy-no-date"),            # no date prefix → full id
        ("2026-06-11-AB12CD", "2026-06-11-AB12CD"),      # uppercase tail → not modern → full id
    ],
)
def test_resume_token(change_id, expected):
    assert resume_token(change_id) == expected
