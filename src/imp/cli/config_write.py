"""`imp config set` / `imp config unset`: pick the layer, edit the file, and land
only bytes imp's own readers accept."""

from __future__ import annotations

import os
import shutil
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from imp.providers.config import (
    parse_config_layer,
    parse_default_base,
    parse_project_facts,
)


def read_back(file: Path, read_path: tuple[str, ...]) -> Any:
    """The value imp's own readers see at `read_path`, or None when absent."""
    head = read_path[0]
    if head == "facts":
        return parse_project_facts(file).get(read_path[1])
    if head == "default-base":
        return parse_default_base(file)
    parsed = parse_config_layer(file)
    node: Any = {} if parsed is None else parsed[0]
    for seg in read_path:
        if not isinstance(node, dict) or seg not in node:
            return None
        node = node[seg]
    return node


def reads_back_as(got: Any, expected: Any) -> bool:
    if isinstance(expected, list):
        return isinstance(got, (list, tuple)) and list(got) == expected
    return type(got) is type(expected) and got == expected


@contextmanager
def scratch_seam(scratch_dir: Path, name: str) -> Iterator[Path]:
    """A real file under the target's own basename, so validator messages already
    name `config.yaml` and `os.replace` lands from the same filesystem."""
    scratch_dir.mkdir(parents=True, exist_ok=True)
    try:
        yield scratch_dir / name
    finally:
        shutil.rmtree(scratch_dir, ignore_errors=True)


def verify_readable(scratch_file: Path, text: str) -> None:
    """Raise LoadError unless imp's layer reader accepts the whole candidate —
    every key, not only the one being written."""
    scratch_file.write_text(text)
    parse_config_layer(scratch_file)


def verify(scratch_file: Path, text: str, read_path: tuple[str, ...],
           expected: Any) -> bool:
    verify_readable(scratch_file, text)
    return reads_back_as(read_back(scratch_file, read_path), expected)


def land(scratch_file: Path, target: Path) -> None:
    # The scratch file is born at the process umask; a target the user tightened
    # must not come back 644.
    if target.exists():
        os.chmod(scratch_file, stat.S_IMODE(target.stat().st_mode))
    os.replace(scratch_file, target)
