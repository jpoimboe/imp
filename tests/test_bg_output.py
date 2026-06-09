# tests/test_bg_output.py
from __future__ import annotations

import json
from pathlib import Path

import anyio
import pytest

import imp.agents.tools.bash  # noqa: F401
from imp.agents.tools import get_registry
from imp.agents.tools.bg import BgProc
from tests.test_bg_spawn import _cleanup, _ctx, _spawn


class _FakeProc:
    """Stand-in process handle for read-path tests (no real child)."""

    def __init__(self, returncode: int | None = None) -> None:
        self.returncode = returncode
        self.pid = 999999

    async def wait(self) -> int:
        return self.returncode if self.returncode is not None else 0


def _fake_bp(tmp_path: Path, ctx, data: bytes, *, status: str = "running",
             rc: int | None = None) -> BgProc:
    from imp.agents.tools.bg import _bg_dir
    d = _bg_dir(ctx)
    d.mkdir(parents=True, exist_ok=True)
    out, err = d / "bg1.out", d / "bg1.err"
    out.write_bytes(data)
    err.write_bytes(b"")
    bp = BgProc(id="bg1", proc=_FakeProc(rc), out_path=out, err_path=err,
                command="fake", status=status, returncode=rc)
    ctx.background_processes["bg1"] = bp
    return bp


async def _read(ctx, id: str = "bg1", **kw) -> dict:
    raw = await get_registry()["bash_output"].async_invoke(ctx, {"id": id, **kw})
    return json.loads(raw)


def test_incremental_paging_no_duplicate_no_lost_bytes(tmp_path: Path) -> None:
    async def main() -> None:
        ctx = _ctx(tmp_path)
        _fake_bp(tmp_path, ctx, b"x" * 120_000, status="exited", rc=0)
        pages = []
        for _ in range(3):
            r = await _read(ctx)
            pages.append(r["stdout"])
        assert [len(p) for p in pages] == [50_000, 50_000, 20_000]
        assert "".join(pages) == "x" * 120_000
        # first two pages carry the resumable notice with remaining counts
        r1 = await _read(ctx)
        assert r1["stdout"] == ""            # fully drained
        assert "notice" not in r1
    anyio.run(main)


def test_more_output_notice_names_remaining_and_paths(tmp_path: Path) -> None:
    async def main() -> None:
        ctx = _ctx(tmp_path)
        _fake_bp(tmp_path, ctx, b"x" * 60_000)
        r = await _read(ctx)
        assert r["stdout_remaining_bytes"] == 10_000
        assert r["stderr_remaining_bytes"] == 0
        assert "bash_output" in r["notice"]
        assert r["out_path"] and r["err_path"]
        assert not Path(r["out_path"]).is_absolute()
    anyio.run(main)


def test_cjk_page_does_not_inflate(tmp_path: Path) -> None:
    async def main() -> None:
        ctx = _ctx(tmp_path)
        _fake_bp(tmp_path, ctx, ("中" * 20_000).encode(), status="exited", rc=0)
        raw = await get_registry()["bash_output"].async_invoke(ctx, {"id": "bg1"})
        assert "\\u4e2d" not in raw          # ensure_ascii=False
        assert "中" in raw
    anyio.run(main)


def test_multibyte_straddling_cap_boundary_reread(tmp_path: Path) -> None:
    """50_000 % 3 == 2: a pure-CJK stream straddles the cap — the held-back
    prefix is NOT advanced past and the next call re-reads it intact."""
    async def main() -> None:
        ctx = _ctx(tmp_path)
        data = ("中" * 25_000).encode()      # 75_000 bytes
        _fake_bp(tmp_path, ctx, data, status="exited", rc=0)
        r1 = await _read(ctx)
        r2 = await _read(ctx)
        assert r1["stdout"] + r2["stdout"] == "中" * 25_000
        assert "�" not in r1["stdout"] + r2["stdout"]
    anyio.run(main)


def test_multibyte_straddling_eof_midwrite_then_completed(tmp_path: Path) -> None:
    async def main() -> None:
        ctx = _ctx(tmp_path)
        full = "中".encode()
        bp = _fake_bp(tmp_path, ctx, b"ab" + full[:2])     # EOF mid-character
        r1 = await _read(ctx)
        assert r1["stdout"] == "ab"                        # prefix held back
        bp.out_path.write_bytes(b"ab" + full)              # writer completed it
        r2 = await _read(ctx)
        assert r2["stdout"] == "中"
    anyio.run(main)


def test_terminal_flush_does_not_pin_tail_bytes(tmp_path: Path) -> None:
    """Killed mid-character: after exit+EOF the tail decodes with replace and
    the cursor reaches EOF — no endless 'remaining bytes' poll loop."""
    async def main() -> None:
        ctx = _ctx(tmp_path)
        full = "中".encode()
        _fake_bp(tmp_path, ctx, b"ab" + full[:2], status="killed", rc=-9)
        r = await _read(ctx)
        assert r["stdout"] == "ab�"
        assert "notice" not in r           # nothing remaining
        r2 = await _read(ctx)
        assert r2["stdout"] == ""
    anyio.run(main)


def test_binary_garbage_advances_with_replace(tmp_path: Path) -> None:
    async def main() -> None:
        ctx = _ctx(tmp_path)
        _fake_bp(tmp_path, ctx, b"\xff\xfe" * 30_000, status="exited", rc=0)
        total = ""
        for _ in range(4):
            r = await _read(ctx)
            total += r["stdout"]
            if not r.get("stdout_remaining_bytes"):
                break
        assert len(total) == 60_000          # every byte became U+FFFD
    anyio.run(main)


def test_status_transition_and_returncode_capture(tmp_path: Path) -> None:
    async def main() -> None:
        ctx = _ctx(tmp_path)
        out = await _spawn(ctx, "echo done; exit 7")
        bp = ctx.background_processes[out["id"]]
        with anyio.fail_after(10):
            await bp.proc.wait()
        r = await _read(ctx, out["id"])
        assert r["status"] == "exited"
        assert r["returncode"] == 7
        assert r["stdout"] == "done\n"
        # an exited id REMAINS readable (registry + files persist to teardown)
        r2 = await _read(ctx, out["id"])
        assert r2["status"] == "exited"
        await _cleanup(ctx)
    anyio.run(main)


def test_unknown_id_raises_value_error(tmp_path: Path) -> None:
    async def main() -> None:
        ctx = _ctx(tmp_path)
        with pytest.raises(ValueError):
            await _read(ctx, "bg42")
    anyio.run(main)


def test_non_teardown_read_error_surfaces_as_tool_error(tmp_path: Path) -> None:
    """Spec error-handling: scratch-file read errors OTHER THAN the teardown
    race (FileNotFoundError) surface as tool errors, consistent with existing
    tools — only the teardown race gets the tombstone fallthrough."""
    import os as _os
    if _os.geteuid() == 0:
        pytest.skip("chmod fault injection needs non-root")

    async def main() -> None:
        ctx = _ctx(tmp_path)
        bp = _fake_bp(tmp_path, ctx, b"data")
        bp.out_path.chmod(0o000)
        try:
            with pytest.raises(PermissionError):
                await _read(ctx)
        finally:
            bp.out_path.chmod(0o644)
    anyio.run(main)


def test_teardown_race_returns_tombstone_not_read_error(tmp_path: Path) -> None:
    """Stream file deleted out from under a registered entry whose in-memory
    state was already flipped (teardown step 1 runs first): bash_output falls
    through to the in-memory tombstone — never a raw read error."""
    async def main() -> None:
        ctx = _ctx(tmp_path)
        bp = _fake_bp(tmp_path, ctx, b"data", status="killed", rc=None)
        bp.kill_reason = "interrupted"
        bp.out_path.unlink()
        bp.err_path.unlink()
        r = await _read(ctx)
        assert r["status"] == "killed"
        assert r["kill_reason"] == "interrupted"
        assert r["stdout"] == ""
    anyio.run(main)
