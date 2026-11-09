"""Every git-tracked .py/.sh under src/ and scripts/ carries the SPDX line: first
line, or second after a shebang. The copyright line is deliberately unchecked."""
from __future__ import annotations

import pytest

from tests._corpus import tracked

SPDX = "# SPDX-License-Identifier: MIT"


def has_spdx_header(text: str) -> bool:
    lines = text.splitlines()
    idx = 1 if lines and lines[0].startswith("#!") else 0
    return len(lines) > idx and lines[idx] == SPDX


@pytest.mark.parametrize(("text", "ok"), [
    ("import os\n", False),
    ("", False),
    (f"{SPDX}\n# Copyright 2026 Josh Poimboeuf\nimport os\n", True),
    (f"#!/usr/bin/env bash\n{SPDX}\n# Copyright 2026 Josh Poimboeuf\n", True),
    ("#!/usr/bin/env bash\nset -e\n", False),
    (f"#!/usr/bin/env python3\n# Copyright 2026 Someone\n{SPDX}\n", False),
    (f'"""doc"""\n# Copyright 2026 Someone\n{SPDX}\n', False),
    (f"{SPDX}\n# Copyright 2026 Someone Else\n", True),
    (f"{SPDX}\nimport os\n", True),
    ("# SPDX-License-Identifier: GPL-2.0\n", False),
])
def test_has_spdx_header(text: str, ok: bool) -> None:
    assert has_spdx_header(text) is ok


def test_covered_files_carry_spdx_header() -> None:
    offenders = [str(p) for p in tracked("src", "scripts", suffixes=(".py", ".sh"))
                 if not has_spdx_header(p.read_text())]
    assert not offenders, (
        'missing SPDX header (expected "# SPDX-License-Identifier: MIT" as the first '
        "line, or the second after a shebang):\n" + "\n".join(offenders))
