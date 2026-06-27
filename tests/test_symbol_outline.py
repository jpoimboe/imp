from __future__ import annotations

from pathlib import Path

from imp.agents.tools.symbol_outline import Symbol, count_phrase, count_symbols, detect_language, extract_symbols, render_file


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
