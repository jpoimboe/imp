"""Language-agnostic structural outline of a source file."""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from tree_sitter_language_pack import (
    ProcessConfig,
    StructureItem,
    detect_language_from_path,
    has_language,
    process,
)

from imp.agents.tools.limits import OUTPUT_TRUNCATION_MARKER, TOOL_OUTPUT_CAP_BYTES

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
    children: tuple[Symbol, ...] = ()


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


# Process-lifetime (per-run) parse cache keyed on (resolved path, mtime_ns).
_PARSE_CACHE: dict[tuple[str, int], list[Symbol]] = {}


def extract_cached(path: Path, source: str, language: str) -> list[Symbol]:
    """`extract_symbols` for an on-disk file, cached on (path, mtime_ns). Callers pass
    the already-read `source` so the cache never re-reads."""
    key = (str(path.resolve()), path.stat().st_mtime_ns)
    cached = _PARSE_CACHE.get(key)
    if cached is not None:
        return list(cached)
    symbols = extract_symbols(source, language)
    _PARSE_CACHE[key] = symbols
    return list(symbols)


def count_symbols(symbols: Sequence[Symbol]) -> tuple[int, int, int]:
    """(fn, data, other) totals over the whole symbol tree, recursive."""
    fn = data = other = 0
    for s in symbols:
        if s.bucket == "fn":
            fn += 1
        elif s.bucket == "data":
            data += 1
        else:
            other += 1
        cfn, cdata, cother = count_symbols(s.children)
        fn += cfn
        data += cdata
        other += cother
    return fn, data, other


def count_phrase(fn: int, data: int, other: int) -> str:
    """The count phrase after the header dash: 'N fn, M data' (zero side omitted),
    or a single 'K symbol(s)' total when no recognized kind is present."""
    if fn == 0 and data == 0:
        total = other
        return f"{total} symbol" if total == 1 else f"{total} symbols"
    parts: list[str] = []
    if fn:
        parts.append(f"{fn} fn")
    if data:
        parts.append(f"{data} data")
    return ", ".join(parts)


def _max_lineno(symbols: Sequence[Symbol]) -> int:
    m = 0
    for s in symbols:
        m = max(m, s.lineno, _max_lineno(s.children))
    return m


def _render_lines(symbols: Sequence[Symbol], out: list[str], depth: int, width: int) -> None:
    for s in symbols:
        out.append(f"{s.lineno:>{width}}: {'  ' * depth}{s.signature}")
        _render_lines(s.children, out, depth + 1, width)


def render_file(display: str, symbols: Sequence[Symbol], *, detail: str = "signatures") -> str:
    """A file's outline: count header, then '<lineno>: <signature>' lines
    (detail='signatures'), nesting by indentation; `display` is the worktree-relative path."""
    fn, data, other = count_symbols(symbols)
    header = f"{display} — {count_phrase(fn, data, other)}"
    if detail == "counts" or not symbols:
        return header
    width = len(str(_max_lineno(symbols)))
    body: list[str] = []
    _render_lines(symbols, body, 0, width)
    full = "\n".join([header, *body])
    if len(full.encode()) <= TOOL_OUTPUT_CAP_BYTES:
        return full
    return _thin_file(header, body)


def _thin_file(header: str, body: list[str]) -> str:
    """Header plus as many leading symbol lines as the budget allows, then a '… +N
    not shown' notice and the truncation marker."""
    marker = OUTPUT_TRUNCATION_MARKER.strip()
    kept: list[str] = []
    size = len(header.encode())
    for i, line in enumerate(body):
        notice = f"… +{len(body) - i} not shown"
        # +3 accounts for the three join newlines: after header, after this line, after notice.
        line_bytes = len(line.encode()) + len(notice.encode()) + len(marker.encode()) + 3
        if size + line_bytes > TOOL_OUTPUT_CAP_BYTES:
            break
        kept.append(line)
        size += len(line.encode()) + 1
    omitted = len(body) - len(kept)
    return "\n".join([header, *kept, f"… +{omitted} not shown", marker])
