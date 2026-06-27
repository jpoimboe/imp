"""Language-agnostic structural outline of a source file."""

from __future__ import annotations

from pathlib import Path

from tree_sitter_language_pack import (
    detect_language_from_path,
    has_language,
)

# Filenames the pack's path-based detector misses but whose grammars exist.
_FILENAME_LANGS = {"Makefile": "make", "Dockerfile": "dockerfile"}


def detect_language(path: Path) -> str | None:
    """The tree-sitter language name for `path`, or None when unsupported.

    Extension-based via the pack's detector, with a filename fallback for a few
    grammars it does not key on filename. Returns a name only when the grammar
    is actually present (`has_language`)."""
    lang: str | None = detect_language_from_path(str(path))
    if lang is None:
        lang = _FILENAME_LANGS.get(path.name)
    if lang is not None and has_language(lang):
        return lang
    return None
