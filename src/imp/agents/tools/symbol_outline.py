"""Language-agnostic structural outline of a source file."""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass
from pathlib import Path

from tree_sitter_language_pack import (
    ProcessConfig,
    StructureItem,
    detect_language_from_path,
    has_language,
    process,
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


# StructureKind (lowercased str) -> language-agnostic bucket.
_KIND_BUCKET: dict[str, str] = {
    "function": "fn",
    "method": "fn",
    "class": "data",
    "struct": "data",
    "enum": "data",
    "interface": "data",
    "trait": "data",
    "impl": "data",
    "module": "data",
    "namespace": "data",
}

_WHITESPACE = re.compile(r"\s+")

_STRUCTURE_CONFIG = ProcessConfig(
    language="",  # replaced per call via dataclasses.replace
    structure=True,
    imports=False,
    exports=False,
    comments=False,
    docstrings=False,
    symbols=False,
    diagnostics=False,
    data_extraction=False,
)


@dataclass(frozen=True)
class Symbol:
    name: str
    bucket: str  # "fn" | "data" | "other"
    lineno: int  # 1-based; 0 if the span is unavailable
    signature: str
    children: tuple["Symbol", ...] = ()


def _signature(source_bytes: bytes, item: StructureItem) -> str:
    """Declaration text with the body elided, collapsed to one line."""
    span = item.span
    body = item.body_span
    if span is None:
        return ""
    start = span.start_byte
    end = body.start_byte if body is not None else span.end_byte
    raw = source_bytes[start:end].decode("utf-8", "replace")
    return _WHITESPACE.sub(" ", raw).strip()


def _to_symbol(source_bytes: bytes, item: StructureItem) -> Symbol:
    span = item.span
    kind = str(item.kind).lower() if item.kind is not None else ""
    return Symbol(
        name=item.name or "",
        bucket=_KIND_BUCKET.get(kind, "other"),
        lineno=(span.start_line + 1) if span is not None else 0,
        signature=_signature(source_bytes, item),
        children=tuple(_to_symbol(source_bytes, c) for c in item.children),
    )


def extract_symbols(source: str, language: str) -> list[Symbol]:
    """Top-level Symbol tree for `source`, parsed as `language`. Never raises on
    malformed source — the pack returns an empty structure."""
    config = dataclasses.replace(_STRUCTURE_CONFIG, language=language)
    result = process(source, config)
    source_bytes = source.encode("utf-8")
    return [_to_symbol(source_bytes, item) for item in result.structure]
