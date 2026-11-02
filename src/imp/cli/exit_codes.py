# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""The process exit codes a script reads from imp's exit status."""

from __future__ import annotations

from enum import IntEnum


class ExitCode(IntEnum):
    OK = 0
    FAILED = 1
    USAGE = 2
    STARTUP_FAILED = 3
    LOST_CONNECTION = 4
    # Left, paused, saved, shelved, discarded, abandoned or cancelled: nothing failed.
    USER_ENDED = 130
