from __future__ import annotations

import os
import subprocess
from pathlib import Path

import anyio

import imp.agents.tools.symbol_outline as so
from imp.agents.tools.symbol_outline import (
    Symbol,
    count_phrase,
    count_symbols,
    detect_language,
    extract_symbols,
    render_file,
)


def test_detect_language_by_extension() -> None:
    assert detect_language(Path("pkg/foo.py")) == "python"
    assert detect_language(Path("a/b/foo.ts")) == "typescript"
    assert detect_language(Path("svc/main.go")) == "go"
    assert detect_language(Path("src/lib.rs")) == "rust"


def test_detect_language_filename_fallback() -> None:
    assert detect_language(Path("Makefile")) == "make"
    assert detect_language(Path("build/Dockerfile")) == "dockerfile"


def test_detect_language_unknown_returns_none() -> None:
    assert detect_language(Path("notes.unknownext")) is None
    assert detect_language(Path("LICENSE")) is None


_PY = (
    "import os\n"
    "\n"
    "\n"
    "class Foo:\n"
    "    def method(self, a: int) -> str:\n"
    '        return "x"\n'
    "\n"
    "\n"
    "def run_change(change, config=None) -> int:\n"
    "    return 1\n"
)


def test_extract_python_nesting_and_signatures() -> None:
    syms = extract_symbols(_PY, "python")
    assert [s.name for s in syms] == ["Foo", "run_change"]

    foo = syms[0]
    assert foo.bucket == "data"
    assert foo.lineno == 4
    assert foo.signature == "class Foo:"
    assert [c.name for c in foo.children] == ["method"]

    method = foo.children[0]
    assert method.bucket == "fn"
    assert method.lineno == 5
    assert method.signature == "def method(self, a: int) -> str:"

    run = syms[1]
    assert run.bucket == "fn"
    assert run.lineno == 9
    assert run.signature == "def run_change(change, config=None) -> int:"
    assert run.children == ()


def test_extract_typescript() -> None:
    ts = (
        "export interface User {\n"
        "    id: number;\n"
        "}\n"
        "\n"
        "export class Service {\n"
        "    constructor(private x: number) {}\n"
        "\n"
        "    getId(): number {\n"
        "        return this.x;\n"
        "    }\n"
        "}\n"
        "\n"
        "export function add(a: number, b: number): number {\n"
        "    return a + b;\n"
        "}\n"
    )
    syms = extract_symbols(ts, "typescript")
    assert [(s.name, s.bucket, s.lineno) for s in syms] == [
        ("User", "data", 1),
        ("Service", "data", 5),
        ("add", "fn", 13),
    ]
    assert syms[0].signature == "interface User"
    assert syms[2].signature == "function add(a: number, b: number): number"
    assert [c.name for c in syms[1].children] == ["constructor", "getId"]
    assert syms[1].children[1].signature == "getId(): number"


def test_extract_go() -> None:
    go = (
        "package main\n"
        "\n"
        "type Point struct {\n"
        "\tX int\n"
        "}\n"
        "\n"
        "func (p Point) Dist() int { return p.X }\n"
        "\n"
        "func Add(a int, b int) int { return a + b }\n"
    )
    syms = extract_symbols(go, "go")
    assert [(s.name, s.bucket, s.lineno) for s in syms] == [
        ("Dist", "fn", 7),
        ("Add", "fn", 9),
    ]
    assert syms[0].signature == "func (p Point) Dist() int"


def test_extract_rust() -> None:
    rust = (
        "pub struct Point {\n"
        "    x: i32,\n"
        "}\n"
        "\n"
        "enum Color {\n"
        "    Red,\n"
        "}\n"
        "\n"
        "trait Shape {\n"
        "    fn area(&self) -> f64;\n"
        "}\n"
        "\n"
        "impl Point {\n"
        "    fn new() -> Self {\n"
        "        Point { x: 0 }\n"
        "    }\n"
        "}\n"
        "\n"
        "pub fn add(a: i32, b: i32) -> i32 {\n"
        "    a + b\n"
        "}\n"
    )
    syms = extract_symbols(rust, "rust")
    assert [(s.name, s.bucket, s.lineno) for s in syms] == [
        ("Point", "data", 1),
        ("Color", "data", 5),
        ("Shape", "data", 9),
        ("Point", "data", 13),
        ("add", "fn", 19),
    ]
    assert syms[3].children[0].name == "new"
    assert syms[3].children[0].signature == "fn new() -> Self"
    assert syms[4].signature == "pub fn add(a: i32, b: i32) -> i32"


def test_extract_empty_and_unparseable_yield_no_symbols() -> None:
    # The pack returns an empty structure (never raises) on empty or broken input.
    assert extract_symbols("", "python") == []
    assert extract_symbols("def (((", "python") == []


def test_symbol_is_frozen() -> None:
    s = Symbol(name="x", bucket="fn", lineno=1, signature="def x()")
    assert s.children == ()


def test_count_symbols_is_recursive() -> None:
    tree = [
        Symbol("a", "fn", 1, "def a()", (Symbol("b", "data", 2, "class b"),)),
        Symbol("c", "fn", 5, "def c()"),
    ]
    assert count_symbols(tree) == (2, 1, 0)


def test_count_symbols_on_extracted_python() -> None:
    assert count_symbols(extract_symbols(_PY, "python")) == (2, 1, 0)


def test_count_phrase_both_buckets() -> None:
    assert count_phrase(2, 1, 0) == "2 fn, 1 data"


def test_count_phrase_omits_zero_side() -> None:
    assert count_phrase(2, 0, 0) == "2 fn"
    assert count_phrase(0, 3, 0) == "3 data"


def test_count_phrase_other_silenced_when_recognized_present() -> None:
    assert count_phrase(2, 0, 3) == "2 fn"
    assert count_phrase(0, 1, 4) == "1 data"


def test_count_phrase_total_only_when_unrecognized() -> None:
    # No recognized fn/data kinds at all -> a single total, never a wrong label.
    assert count_phrase(0, 0, 5) == "5 symbols"
    assert count_phrase(0, 0, 1) == "1 symbol"


def test_render_file_signatures() -> None:
    out = render_file("pkg/foo.py", extract_symbols(_PY, "python"))
    assert out == (
        "pkg/foo.py — 2 fn, 1 data\n"
        "4: class Foo:\n"
        "5:   def method(self, a: int) -> str:\n"
        "9: def run_change(change, config=None) -> int:"
    )


def test_render_file_counts_detail_is_header_only() -> None:
    out = render_file("pkg/foo.py", extract_symbols(_PY, "python"), detail="counts")
    assert out == "pkg/foo.py — 2 fn, 1 data"


def test_render_file_unknown_detail_defaults_to_signatures() -> None:
    syms = extract_symbols(_PY, "python")
    assert render_file("pkg/foo.py", syms, detail="bogus") == render_file(
        "pkg/foo.py", syms
    )


def test_render_file_line_number_alignment() -> None:
    # Two-digit and one-digit line numbers right-align to width 2.
    tree = [
        Symbol("a", "fn", 3, "def a()"),
        Symbol("big", "fn", 12, "def big()"),
    ]
    out = render_file("x.py", tree)
    assert out == "x.py — 2 fn\n 3: def a()\n12: def big()"


def test_render_file_width_from_child_lineno() -> None:
    # The deepest child's lineno (100) exceeds all top-level linenos, so the
    # right-align width must come from it (3), not from the top-level max (1).
    tree = [
        Symbol("tiny", "fn", 1, "def tiny():"),
        Symbol(
            "Big",
            "data",
            3,
            "class Big:",
            (Symbol("method_z", "fn", 100, "def method_z():"),),
        ),
    ]
    out = render_file("x.py", tree)
    assert out == (
        "x.py — 2 fn, 1 data\n"
        "  1: def tiny():\n"
        "  3: class Big:\n"
        "100:   def method_z():"
    )


def test_render_file_empty_symbol_list() -> None:
    assert render_file("x.py", []) == "x.py — 0 symbols"


def test_render_file_thins_over_budget(monkeypatch) -> None:
    monkeypatch.setattr(so, "TOOL_OUTPUT_CAP_BYTES", 120)
    tree = [Symbol(f"fn_{i}", "fn", i + 1, f"def fn_{i}()") for i in range(40)]
    out = so.render_file("big.py", tree)

    assert out.splitlines()[0] == "big.py — 40 fn"
    # The whole point of thinning: the result stays within the budget.
    assert len(out) <= 120
    assert out.endswith("... [output truncated at 50 KB] ...")
    # The omission count is honest: kept signature lines + N not shown == 40.
    kept = sum(1 for line in out.splitlines() if "def fn_" in line)
    notice = next(line for line in out.splitlines() if "not shown" in line)
    n_not_shown = int(notice.split("+")[1].split()[0])
    assert 0 < n_not_shown < 40
    assert kept + n_not_shown == 40
    # Trailing symbols were the ones dropped.
    assert "def fn_39()" not in out


def test_render_file_thins_with_tiny_budget(monkeypatch) -> None:
    # Pathological budget below even one line: still returns header + notice +
    # marker (never raises; read_file is the floor).
    monkeypatch.setattr(so, "TOOL_OUTPUT_CAP_BYTES", 10)
    tree = [Symbol(f"fn_{i}", "fn", i + 1, f"def fn_{i}()") for i in range(5)]
    out = so.render_file("big.py", tree)
    assert "… +5 not shown" in out
    assert out.endswith("... [output truncated at 50 KB] ...")


def test_render_file_no_thinning_when_under_budget() -> None:
    out = render_file("pkg/foo.py", extract_symbols(_PY, "python"))
    assert "not shown" not in out
    assert "output truncated" not in out


def test_extract_cached_parses_unchanged_file_once(tmp_path, monkeypatch) -> None:
    f = tmp_path / "m.py"
    f.write_text(_PY)

    calls = {"n": 0}
    real_process = so.process

    def counting_process(source, config):
        calls["n"] += 1
        return real_process(source, config)

    monkeypatch.setattr(so, "process", counting_process)

    first = so.extract_cached(f, _PY, "python")
    second = so.extract_cached(f, _PY, "python")
    assert calls["n"] == 1
    assert [s.name for s in first] == [s.name for s in second] == ["Foo", "run_change"]


def test_extract_cached_reparses_after_mtime_change(tmp_path, monkeypatch) -> None:
    f = tmp_path / "m.py"
    f.write_text(_PY)

    calls = {"n": 0}
    real_process = so.process

    def counting_process(source, config):
        calls["n"] += 1
        return real_process(source, config)

    monkeypatch.setattr(so, "process", counting_process)

    so.extract_cached(f, _PY, "python")
    stat = f.stat()
    os.utime(f, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    so.extract_cached(f, _PY, "python")
    assert calls["n"] == 2


def test_enumerate_dir_walk_fallback_excludes_control_dirs(tmp_path, monkeypatch) -> None:
    (tmp_path / "a.py").write_text("x")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "b.py").write_text("y")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "c.py").write_text("z")
    (tmp_path / ".imp").mkdir()
    (tmp_path / ".imp" / "d.py").write_text("w")

    async def _no_repo(_p):
        return None

    monkeypatch.setattr(so, "repo_root", _no_repo)
    # _enumerate_dir resolves its target and returns resolved paths, so assert
    # against the RESOLVED root (a symlinked TMPDIR, e.g. macOS, otherwise breaks
    # relative_to) — the both-sides-resolved pattern from tests/test_ignore.py.
    root = tmp_path.resolve()
    files = anyio.run(so._enumerate_dir, root, root)
    rels = sorted(p.relative_to(root).as_posix() for p in files)
    assert rels == ["a.py", "pkg/b.py"]


def test_enumerate_dir_gitignore_aware(tmp_path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text("ignored/\n")
    (tmp_path / "keep.py").write_text("x")
    (tmp_path / "ignored").mkdir()
    (tmp_path / "ignored" / "secret.py").write_text("s")

    root = tmp_path.resolve()
    files = anyio.run(so._enumerate_dir, root, root)
    rels = sorted(p.relative_to(root).as_posix() for p in files)
    assert "keep.py" in rels
    assert ".gitignore" in rels
    assert "ignored/secret.py" not in rels
    assert all(not r.startswith(".git/") for r in rels)


def test_scan_file_python(tmp_path) -> None:
    f = tmp_path / "foo.py"
    f.write_text(_PY)
    e = anyio.run(so.scan_file, f, tmp_path)
    assert e.rel == "foo.py"
    assert [s.name for s in e.symbols] == ["Foo", "run_change"]


def test_scan_file_unsupported_extension_has_no_symbols(tmp_path) -> None:
    f = tmp_path / "notes.txt"
    f.write_text("hello world")
    e = anyio.run(so.scan_file, f, tmp_path)
    assert e.symbols == ()


def test_scan_file_supported_but_empty_outline(tmp_path) -> None:
    # A grammar exists for Makefile but it yields zero structure items, so the
    # entry has no symbols — downstream this renders as "(no outline)".
    f = tmp_path / "Makefile"
    f.write_text("all:\n\techo hi\n")
    e = anyio.run(so.scan_file, f, tmp_path)
    assert e.symbols == ()


def test_scan_file_swallows_parse_failure(tmp_path, monkeypatch) -> None:
    f = tmp_path / "foo.py"
    f.write_text(_PY)

    def _boom(*_a, **_k):
        raise RuntimeError("parse exploded")

    monkeypatch.setattr(so, "extract_cached", _boom)
    e = anyio.run(so.scan_file, f, tmp_path)
    assert e.rel == "foo.py"
    assert e.symbols == ()


def _render_entries():
    foo = so.FileEntry(
        "pkg/foo.py",
        (
            Symbol("Foo", "data", 4, "class Foo:",
                   (Symbol("method", "fn", 5, "def method(self, a: int) -> str:"),)),
            Symbol("run_change", "fn", 9, "def run_change(change, config=None) -> int:"),
        ),
    )
    util = so.FileEntry("pkg/util.py", (Symbol("helper", "fn", 1, "def helper():"),))
    mk = so.FileEntry("Makefile", ())
    return [mk, foo, util]


def test_render_dir_counts() -> None:
    tree = so.build_tree(".", _render_entries())
    assert so.render_dir(tree, detail="counts") == (
        "./ — 1 dir, 3 files, 4 symbols\n"
        "  Makefile — (no outline)\n"
        "  pkg/ — 2 files, 4 symbols\n"
        "    foo.py — 2 fn, 1 data\n"
        "    util.py — 1 fn"
    )


def test_render_dir_signatures() -> None:
    tree = so.build_tree(".", _render_entries())
    assert so.render_dir(tree, detail="signatures") == (
        "./ — 1 dir, 3 files, 4 symbols\n"
        "  Makefile — (no outline)\n"
        "  pkg/ — 2 files, 4 symbols\n"
        "    foo.py — 2 fn, 1 data\n"
        "      4: class Foo:\n"
        "      5:   def method(self, a: int) -> str:\n"
        "      9: def run_change(change, config=None) -> int:\n"
        "    util.py — 1 fn\n"
        "      1: def helper():"
    )


def test_render_dir_max_depth_collapses_subdir() -> None:
    tree = so.build_tree(".", _render_entries())
    # max_depth=1: list the root's children, but collapse pkg/ to its recursive
    # rollup line (its file lines are not shown, the count still is).
    assert so.render_dir(tree, detail="signatures", max_depth=1) == (
        "./ — 1 dir, 3 files, 4 symbols\n"
        "  Makefile — (no outline)\n"
        "  pkg/ — 2 files, 4 symbols"
    )


def test_render_dir_unknown_detail_defaults_to_signatures() -> None:
    # AC#4: any unrecognized detail value behaves as "signatures" (counts plus
    # signatures), not "counts".
    tree = so.build_tree(".", _render_entries())
    assert so.render_dir(tree, detail="bogus") == so.render_dir(tree, detail="signatures")


def test_build_tree_groups_and_counts_recursively() -> None:
    foo = so.FileEntry(
        "pkg/foo.py",
        (
            Symbol("Foo", "data", 4, "class Foo:",
                   (Symbol("method", "fn", 5, "def method():"),)),
            Symbol("run", "fn", 9, "def run():"),
        ),
    )
    util = so.FileEntry("pkg/sub/util.py", (Symbol("helper", "fn", 1, "def helper():"),))
    mk = so.FileEntry("Makefile", ())
    tree = so.build_tree(".", [mk, foo, util])

    assert tree.rel == "."
    assert [f.rel for f in tree.files] == ["Makefile"]
    assert [d.rel for d in tree.subdirs] == ["pkg"]
    pkg = tree.subdirs[0]
    assert [f.rel for f in pkg.files] == ["pkg/foo.py"]
    assert [d.rel for d in pkg.subdirs] == ["pkg/sub"]
    assert [f.rel for f in pkg.subdirs[0].files] == ["pkg/sub/util.py"]

    # (n_dirs, n_files, n_symbols), recursive over the whole subtree.
    assert so.dir_counts(tree) == (2, 3, 4)
    assert so.dir_counts(pkg) == (1, 2, 4)
