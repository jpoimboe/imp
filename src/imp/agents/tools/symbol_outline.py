"""Language-agnostic structural outline of a file or directory (the symbol_outline tool)."""

from __future__ import annotations

import dataclasses
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio
from tree_sitter_language_pack import (
    ProcessConfig,
    StructureItem,
    detect_language_from_path,
    has_language,
    process,
)

from imp.agents.tools import imp_tool
from imp.agents.tools.context import ToolContext
from imp.agents.tools.ignore import is_ignored, list_unignored, repo_root
from imp.agents.tools.limits import (
    EXCLUDED_DIRS,
    OUTPUT_TRUNCATION_MARKER,
    TOOL_OUTPUT_CAP_BYTES,
    TOOL_WALLCLOCK_TIMEOUT_S,
    read_text_capped,
    tool_checkpoint,
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


def _walk_files(target: Path) -> list[Path]:
    """Regular files under `target`, pruning control/VCS dirs. The no-git
    fallback for _enumerate_dir; not gitignore-aware (git is the gitignore route)."""
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(target):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDED_DIRS]
        base = Path(dirpath)
        for name in filenames:
            p = base / name
            if p.is_file():
                out.append(p)
    return out


async def _enumerate_dir(
    target: Path,
    policy: Any = None,
    coordinator: object | None = None,
    deadline: float | None = None,
) -> list[Path]:
    """Unignored regular files under `target`, sorted. Git-aware via ignore.py
    (so node_modules/.venv/build and every other gitignored path is skipped);
    falls back to a control-dir-pruned walk when git is unavailable. Under an
    active sandbox policy, files failing allows_read are dropped (grep/glob parity).

    `target` is resolved first so its prefix matches git's resolved toplevel and
    the resolved paths list_unignored returns — the same both-sides-resolved
    guard tests/test_ignore.py uses (a symlinked TMPDIR, e.g. macOS's
    /var -> /private/var, otherwise mismatches)."""
    target = target.resolve()
    repo = await repo_root(target)
    files: list[Path]
    if repo is not None and not await is_ignored(repo, target):
        unignored = await list_unignored(repo, target)
        if unignored is not None:
            # git's `-- <target>` already scopes the listing to target; no extra
            # is_relative_to filter (it only re-introduced the resolve mismatch).
            files = [p for p in unignored if p.is_file()]
        else:
            files = _walk_files(target)
    else:
        files = _walk_files(target)
    masking = policy is not None and policy.active
    result: list[Path] = []
    for p in sorted(files):
        await tool_checkpoint(coordinator, deadline)
        if masking and not policy.allows_read(p.resolve()):
            continue
        result.append(p)
    return result


@dataclass(frozen=True)
class FileEntry:
    rel: str  # worktree-relative posix path (absolute for an external target)
    symbols: tuple[Symbol, ...]  # () when unsupported, unparseable, or empty


async def scan_file(
    path: Path,
    root: Path,
    *,
    coordinator: object | None = None,
    deadline: float | None = None,
) -> FileEntry:
    """A FileEntry for one on-disk file: its rel path and parsed symbol tree
    (empty when the language is unsupported or parsing fails). Never raises."""
    rel = path.relative_to(root).as_posix() if path.is_relative_to(root) else path.as_posix()
    lang = detect_language(path)
    if lang is None:
        return FileEntry(rel, ())
    try:
        source, _ = await read_text_capped(path, coordinator=coordinator, deadline=deadline)
        symbols = extract_cached(path, source, lang)
    except Exception:
        return FileEntry(rel, ())
    return FileEntry(rel, tuple(symbols))


@dataclass
class DirNode:
    rel: str  # worktree-relative directory path ("." for the project root)
    files: list[FileEntry]
    subdirs: list[DirNode]


def _entry_counts(entry: FileEntry) -> tuple[int, int, int]:
    return count_symbols(entry.symbols)


def dir_counts(node: DirNode) -> tuple[int, int, int]:
    """(n_dirs, n_files, n_symbols) for the whole subtree under `node`, recursive."""
    n_dirs = len(node.subdirs)
    n_files = len(node.files)
    n_symbols = sum(sum(_entry_counts(f)) for f in node.files)
    for sub in node.subdirs:
        sd, sf, ss = dir_counts(sub)
        n_dirs += sd
        n_files += sf
        n_symbols += ss
    return n_dirs, n_files, n_symbols


def _sort_tree(node: DirNode) -> None:
    node.files.sort(key=lambda f: f.rel)
    node.subdirs.sort(key=lambda d: d.rel)
    for sub in node.subdirs:
        _sort_tree(sub)


def _plural(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _dir_phrase(n_dirs: int, n_files: int, n_symbols: int) -> str:
    """'N dirs, M files, K symbols' for a directory line; the dir term is omitted
    when zero. Always reflects the recursive subtree totals."""
    parts: list[str] = []
    if n_dirs:
        parts.append(_plural(n_dirs, "dir"))
    parts.append(_plural(n_files, "file"))
    parts.append(_plural(n_symbols, "symbol"))
    return ", ".join(parts)


def _file_phrase(entry: FileEntry) -> str:
    if not entry.symbols:
        return "(no outline)"
    return count_phrase(*_entry_counts(entry))


def _dir_text(node: DirNode) -> str:
    nd, nf, ns = dir_counts(node)
    return f"{node.rel}/ — {_dir_phrase(nd, nf, ns)}"


def _basename(rel: str) -> str:
    return rel.rsplit("/", 1)[-1]


def _render_dir_node(
    node: DirNode, lines: list[str], depth: int, detail: str, max_depth: int | None,
) -> None:
    lines.append(f"{'  ' * depth}{_dir_text(node)}")
    if max_depth is not None and depth >= max_depth:
        return
    child_indent = "  " * (depth + 1)
    for f in node.files:
        lines.append(f"{child_indent}{_basename(f.rel)} — {_file_phrase(f)}")
        if detail != "counts" and f.symbols:
            width = len(str(_max_lineno(f.symbols)))
            body: list[str] = []
            _render_lines(f.symbols, body, 0, width)
            for bl in body:
                lines.append(f"{child_indent}  {bl}")
    for sub in node.subdirs:
        _render_dir_node(sub, lines, depth + 1, detail, max_depth)


def render_dir(tree: DirNode, *, detail: str = "signatures", max_depth: int | None = None) -> str:
    """The directory outline: a recursive-count line per directory and a count
    (plus signatures, when detail='signatures') line per file, hierarchy shown by
    indentation. `max_depth` caps how deep the listing expands; a collapsed
    directory still shows its recursive totals."""
    lines: list[str] = []
    _render_dir_node(tree, lines, 0, detail, max_depth)
    return "\n".join(lines)


def _tree_depth(node: DirNode) -> int:
    if not node.subdirs:
        return 0
    return 1 + max(_tree_depth(s) for s in node.subdirs)


def _render_dir_thinned(tree: DirNode, *, detail: str, max_depth: int | None) -> str:
    """render_dir fit to TOOL_OUTPUT_CAP_BYTES by breadth-first thinning: first
    drop per-file signatures (-> counts), then collapse the deepest directories to
    recursive-rollup lines, never omitting a subtree. A thinned result ends with
    OUTPUT_TRUNCATION_MARKER."""
    full = render_dir(tree, detail=detail, max_depth=max_depth)
    if len(full.encode()) <= TOOL_OUTPUT_CAP_BYTES:
        return full
    marker = OUTPUT_TRUNCATION_MARKER.strip()

    def fits(text: str) -> bool:
        return len(text.encode()) + len(marker.encode()) + 1 <= TOOL_OUTPUT_CAP_BYTES

    body = render_dir(tree, detail="counts", max_depth=max_depth)
    if fits(body):
        return f"{body}\n{marker}"
    ceiling = _tree_depth(tree) if max_depth is None else min(max_depth, _tree_depth(tree))
    depth = ceiling
    while depth >= 0:
        body = render_dir(tree, detail="counts", max_depth=depth)
        if fits(body):
            break
        depth -= 1
    return f"{body}\n{marker}"


def build_tree(root_rel: str, entries: list[FileEntry]) -> DirNode:
    """Group `entries` (each carrying its worktree-relative rel) into a directory
    tree rooted at `root_rel`. Intermediate directories become DirNodes whose
    `rel` is the worktree-relative directory path."""
    root = DirNode(rel=root_rel, files=[], subdirs=[])
    nodes: dict[str, DirNode] = {root_rel: root}
    for entry in entries:
        stripped = entry.rel if root_rel == "." else entry.rel[len(root_rel) + 1:]
        parts = stripped.split("/")
        cur = root
        prefix = root_rel
        for comp in parts[:-1]:
            prefix = comp if prefix == "." else f"{prefix}/{comp}"
            child = nodes.get(prefix)
            if child is None:
                child = DirNode(rel=prefix, files=[], subdirs=[])
                nodes[prefix] = child
                cur.subdirs.append(child)
            cur = child
        cur.files.append(entry)
    _sort_tree(root)
    return root


_UNSUPPORTED_FILE = " — no outline (unsupported); read directly"


def _display(target: Path, root: Path) -> str:
    if target.is_relative_to(root):
        rel = target.relative_to(root).as_posix()
        return rel if rel else "."
    return target.as_posix()


async def _outline_file(
    target: Path, root: Path, *, detail: str,
    coordinator: object | None, deadline: float | None,
) -> str:
    disp = _display(target, root)
    lang = detect_language(target)
    if lang is None:
        return f"{disp}{_UNSUPPORTED_FILE}"
    try:
        source, _ = await read_text_capped(target, coordinator=coordinator, deadline=deadline)
        symbols = extract_cached(target, source, lang)
    except Exception:
        return f"{disp}{_UNSUPPORTED_FILE}"
    if not symbols:
        return f"{disp}{_UNSUPPORTED_FILE}"
    return render_file(disp, symbols, detail=detail)


async def _outline_dir(
    target: Path, root: Path, *, detail: str, max_depth: int | None,
    policy: Any, coordinator: object | None, deadline: float | None,
) -> str:
    files = await _enumerate_dir(target, policy, coordinator, deadline)
    disp = _display(target, root)
    if not files:
        return f"{disp} — empty (no files to outline)"
    entries: list[FileEntry] = []
    for p in files:
        entries.append(await scan_file(p, root, coordinator=coordinator, deadline=deadline))
    tree = build_tree(disp, entries)
    return _render_dir_thinned(tree, detail=detail, max_depth=max_depth)


@imp_tool(
    name="symbol_outline",
    description=(
        "Compact structural outline of a file or directory: defined symbols "
        "(functions, classes, types, …) with signatures and 1-based line "
        "numbers, bodies elided, so you can see a file's or subtree's shape "
        "before paying full-read tokens for the few bodies you actually need. "
        "A file path gives a per-file outline; a directory path recurses "
        "gitignore-aware with per-file and recursive per-directory counts. "
        "`detail` is 'signatures' (default: counts plus signatures) or 'counts' "
        "(names and counts only). `max_depth` caps directory recursion depth "
        "(directory paths only; ignored for a file). Paths are relative to the "
        "worktree root, like glob/grep. Read-only and never raises: an "
        "unsupported or unparseable file degrades to a one-line 'read directly' "
        "note, and output is capped at 50 KB with breadth-first thinning. Not a "
        "substitute for read_file — use it to choose what to read."
    ),
    read_only=True,
)
async def symbol_outline(
    ctx: ToolContext, path: str, detail: str = "signatures", max_depth: int | None = None,
) -> str:
    target = ctx.authorize_fs(("read",), path)
    root = ctx.project_root.resolve()
    coordinator = ctx.coordinator
    deadline = anyio.current_time() + TOOL_WALLCLOCK_TIMEOUT_S
    if max_depth is not None and max_depth < 0:
        max_depth = 0
    if target.is_file():
        return await _outline_file(
            target, root, detail=detail, coordinator=coordinator, deadline=deadline)
    if target.is_dir():
        return await _outline_dir(
            target, root, detail=detail, max_depth=max_depth,
            policy=ctx.sandbox_policy, coordinator=coordinator, deadline=deadline)
    return f"{path}: no such file or directory"
