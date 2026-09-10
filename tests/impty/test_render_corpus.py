from __future__ import annotations

import contextlib
import json
import re
from itertools import pairwise
from pathlib import Path
from typing import Any

from impty import view
from impty.driver import ImptyDriver
from impty.screen import TerminalScreen
from impty.stream import TerminalStream

_FIXTURE_DIR = Path(__file__).with_name("fixtures") / "render_corpus"
_MANIFEST = _FIXTURE_DIR / "manifest.json"

_DELETE = re.compile(r"\[(?P<count>\d+) rows? deleted at row (?P<row>\d+)\]")
_CHANGE = re.compile(
    r"\[(?:row (?P<single>\d+)|rows (?P<first>\d+)-(?P<last>\d+))"
    r"(?:(?: replacing (?P<consumed>\d+) rows?)|(?P<inserted> inserted))?\]"
)


class _ReplaySession:
    def __init__(self, cols: int, rows: int) -> None:
        self.screen = TerminalScreen(cols, rows, history=2000, ratio=0.5)
        self._stream = TerminalStream(self.screen)
        self.returncode = None

    @property
    def rows(self) -> int:
        return self.screen.lines

    def feed(self, data: bytes) -> None:
        self._stream.feed(data)

    def paused(self):
        return contextlib.nullcontext()

    def cursor(self) -> tuple[int, int]:
        return view.cursor_pos(self.screen)

    def regime(self):
        return view.regime(self.screen)

    def lines(self, limit: int | None = None) -> list[str]:
        return view.lines(self.screen, limit)


def _body(rendered: str) -> str:
    return rendered.partition("\n")[2]


def _apply_diff(previous: list[str] | None, body: str) -> list[str]:
    if body.startswith("[full screen]\n"):
        return body.removeprefix("[full screen]\n").split("\n")

    assert previous is not None
    if body == "[no text change]":
        return list(previous)

    rebuilt = list(previous)
    lines = body.split("\n")
    index = 0
    while index < len(lines):
        label = lines[index]
        index += 1

        deletion = _DELETE.fullmatch(label)
        if deletion is not None:
            row = int(deletion.group("row"))
            count = int(deletion.group("count"))
            del rebuilt[row : row + count]
            continue

        change = _CHANGE.fullmatch(label)
        assert change is not None, label
        first = int(change.group("single") or change.group("first"))
        last = first if change.group("single") is not None else int(change.group("last"))
        emitted_count = last - first + 1
        if change.group("inserted") is not None:
            consumed_count = 0
        elif change.group("consumed") is not None:
            consumed_count = int(change.group("consumed"))
        else:
            consumed_count = emitted_count

        emitted = lines[index : index + emitted_count]
        assert len(emitted) == emitted_count
        rebuilt[first : first + consumed_count] = emitted
        index += emitted_count

    return rebuilt


def _frame(frames: list[dict[str, Any]], label: str) -> dict[str, Any]:
    matches = [frame for frame in frames if frame["label"] == label]
    assert len(matches) == 1, label
    return matches[0]


def _prompt_row(frame: dict[str, Any]) -> str:
    matches = [row for row in frame["rows"] if row.startswith("> ")]
    assert len(matches) == 1, frame["label"]
    return matches[0]


def _assert_regime_coverage(replayed: dict[str, list[dict[str, Any]]]) -> None:
    live = replayed["1a-imp-live-tty"]
    assert all(frame["regime"] == "scroll" for frame in live)
    assert any(
        before["rows"][:21] != after["rows"][:21]
        and before["rows"][shift:21] == after["rows"][: 21 - shift]
        and any(row.strip() for row in after["rows"][: 21 - shift])
        for before, after in pairwise(live)
        for shift in range(1, 21)
    )
    active_live = [frame for frame in live if frame["cursor"] == (78, 23)]
    assert len(active_live) >= 8
    assert all("thinking" in frame["rows"][21] for frame in active_live)
    assert len({frame["rows"][21][0] for frame in active_live}) >= 4
    assert all(
        frame["rows"][23].split(" · ")[:2] == ["2026-06-06-ch4buk", "add-version-flag"]
        for frame in active_live
    )

    scroll = replayed["1b-imp-stream-scroll"]
    assert all(frame["regime"] == "scroll" for frame in scroll)
    assert any(
        before["rows"][3:23] == after["rows"][:20]
        and before["rows"][23] == after["rows"][23]
        and before["rows"][20:23] != after["rows"][20:23]
        for before, after in pairwise(scroll)
    )

    prompt = replayed["2-prompt-toolkit"]
    assert [
        _prompt_row(_frame(prompt, "send#0:'Add a top-level '")),
        _prompt_row(_frame(prompt, "send#1:'--version'")),
        _prompt_row(_frame(prompt, "send#2:' flag'")),
    ] == [
        "> Add a top-level",
        "> Add a top-level --version",
        "> Add a top-level --version flag",
    ]

    vim = replayed["3-vim-altscreen"]
    assert [frame["regime"] for frame in vim[:5]] == ["screen"] * 5
    cursor_rows = [frame["cursor"][1] for frame in vim[1:5]]
    assert cursor_rows[0] < cursor_rows[1] < cursor_rows[2]
    assert cursor_rows[3] == 0
    assert (
        sum(before != after for before, after in zip(vim[3]["rows"], vim[4]["rows"], strict=True))
        >= 20
    )
    assert vim[4]["body"].startswith("[full screen]\n")

    top = replayed["4-top-altscreen"]
    assert all(frame["rows"][0].startswith("top - ") for frame in top)
    assert all(any("PID USER" in row for row in frame["rows"]) for frame in top)
    full_frame_refreshes = [
        frame for frame in top[1:] if len(frame["raw"]) >= len("\n".join(frame["rows"]).encode())
    ]
    assert len(full_frame_refreshes) >= 8
    assert sum(before["rows"] != after["rows"] for before, after in pairwise(top)) >= 8


def test_the_frozen_corpus_is_reconstructible_bounded_and_smaller() -> None:
    manifest: dict[str, Any] = json.loads(_MANIFEST.read_text())
    corpora = manifest["corpora"]
    assert [(corpus["name"], len(corpus["steps"])) for corpus in corpora] == [
        ("1a-imp-live-tty", 23),
        ("1b-imp-stream-scroll", 21),
        ("2-prompt-toolkit", 22),
        ("3-vim-altscreen", 25),
        ("4-top-altscreen", 17),
    ]

    full_body_bytes = 0
    changed_body_bytes = 0
    replayed: dict[str, list[dict[str, Any]]] = {}

    for corpus in corpora:
        raw = (_FIXTURE_DIR / corpus["raw"]).read_bytes()
        session = _ReplaySession(manifest["cols"], manifest["rows"])
        driver = ImptyDriver()
        previous: list[str] | None = None
        frames: list[dict[str, Any]] = []
        offset = 0

        for step in corpus["steps"]:
            next_offset = step["offset"]
            assert offset <= next_offset <= len(raw)
            chunk = raw[offset:next_offset]
            session.feed(chunk)
            offset = next_offset

            current = session.lines(limit=session.rows)
            full_body = "\n".join(current)
            if step["render"] == "diff":
                body = _body(driver._render_diff(session))
                rebuilt = _apply_diff(previous, body)
                if body == "[no text change]":
                    assert previous == current
                elif body.startswith("[full screen]\n"):
                    assert body == f"[full screen]\n{full_body}"
                else:
                    assert len(body) < len(full_body)
            elif step["render"] == "view":
                driver = ImptyDriver()
                body = _body(driver._render(session))
                assert body == full_body
                rebuilt = body.split("\n")
                assert _body(driver._render_diff(session)) == "[no text change]"
            else:
                raise AssertionError(f"unknown render route: {step['render']}")

            assert rebuilt == current, f"{corpus['name']} {step['label']}"
            frames.append(
                {
                    "label": step["label"],
                    "rows": current,
                    "body": body,
                    "cursor": session.cursor(),
                    "regime": session.regime().value,
                    "raw": chunk,
                }
            )
            previous = rebuilt
            full_body_bytes += len(full_body.encode())
            changed_body_bytes += len(body.encode())

        assert offset == len(raw)
        replayed[corpus["name"]] = frames

    _assert_regime_coverage(replayed)
    saving_fraction = round(1 - changed_body_bytes / full_body_bytes, 10)
    assert full_body_bytes == manifest["full_body_bytes"]
    assert changed_body_bytes == manifest["changed_body_bytes"]
    assert saving_fraction == manifest["saving_fraction"]
    assert saving_fraction > 0.45
