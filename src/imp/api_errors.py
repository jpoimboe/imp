# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""The exception hierarchy `imp.api` exports, apart from it so `remote.session.attach`
can re-parent DaemonStartupError without an import cycle."""

from __future__ import annotations


class ImpError(Exception):
    """Base for every error the embedding API raises."""


class QuestNotFound(ImpError):
    """No quest with that id under this control root."""


class DaemonUnreachable(ImpError):
    """A quest's session daemon could not be reached or brought up."""


class NotAtGate(ImpError):
    """Input was sent while no gate is open."""


class InvalidVar(ImpError):
    """A variable override the workflow does not declare, or whose type it rejects."""
