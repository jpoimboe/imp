from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Protocol


class _HasLines(Protocol):
    def lines(self, limit: int | None = None) -> list[str]: ...


def assert_screen(
    view: _HasLines,
    *,
    contains: Iterable[str] = (),
    excludes: Iterable[str] = (),
    patterns: Iterable[str] = (),
    limit: int | None = None,
) -> None:
    body = "\n".join(view.lines(limit=limit))
    for sub in contains:
        assert sub in body, f"expected {sub!r} in screen:\n{body}"
    for sub in excludes:
        assert sub not in body, f"expected {sub!r} ABSENT from screen:\n{body}"
    for pat in patterns:
        assert re.search(pat, body), f"expected /{pat}/ to match screen:\n{body}"
