# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""Durable file publication primitives."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import BinaryIO


def _fsync_file(stream: BinaryIO, path: Path) -> None:
    stream.flush()
    os.fsync(stream.fileno())


def fsync_file(path: Path) -> None:
    with path.open("rb") as stream:
        _fsync_file(stream, path)


def fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def durable_replace(
    path: Path,
    writer: Callable[[BinaryIO], object],
) -> None:
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.stem}-", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            writer(stream)
            _fsync_file(stream, temp_path)
        os.replace(temp_path, path)
        fsync_directory(path.parent)
    except BaseException:
        with suppress(FileNotFoundError):
            temp_path.unlink()
        raise


def durable_replace_bytes(path: Path, data: bytes) -> None:
    durable_replace(path, lambda stream: stream.write(data))
