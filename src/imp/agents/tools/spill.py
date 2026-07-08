"""Spill-to-disk on truncation (spec: workstream E).

Writes an over-cap tool output to a file under the run directory and returns a
byte-safe preview plus a recoverable-path notice, so a truncated `bash` /
`sub_agent` tail is recoverable with an ordinary `read_file`. Runtime imports
are stdlib-only plus the sibling `limits` tunable; `ToolContext` is referenced
under TYPE_CHECKING to stay import-cycle-free.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from imp.agents.tools.limits import PREVIEW_BYTES

if TYPE_CHECKING:
    from imp.agents.tools.context import ToolContext


def spill_tool_output(
    ctx: ToolContext, name: str, full: str, *, preview_bytes: int = PREVIEW_BYTES,
) -> str:
    """Write `full` to <run>/tool-results/<name> and return a preview + notice.

    The preview is the first `preview_bytes` ENCODED UTF-8 bytes, decoded with
    errors="ignore" so a partial trailing character is dropped rather than
    corrupted. With preview_bytes=0 the notice is returned alone. The notice's
    path is project_root-relative (bg.py's out_path/err_path convention), so the
    agent's worktree-relative read_file resolves it directly.
    """
    spill_dir = ctx.events_path.parent / "tool-results"
    spill_dir.mkdir(parents=True, exist_ok=True)
    path = spill_dir / name
    data = full.encode("utf-8")
    path.write_bytes(data)

    relpath = os.path.relpath(path.resolve(), ctx.project_root.resolve())
    notice = (f"[full output {round(len(data) / 1000)} KB at {relpath}; "
              "read with read_file offset=1]")
    if preview_bytes <= 0:
        return notice
    preview = data[:preview_bytes].decode("utf-8", "ignore")
    return f"{preview}\n{notice}"
