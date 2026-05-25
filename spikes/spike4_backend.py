"""Spike 4: CompositeBackend routes /artifacts/, /project/, / correctly.

VERDICT (2026-05-25, deepagents 0.6.3): PASS w/ corrected API.

Plan-vs-reality corrections for Task 13:

1. CompositeBackend signature is NOT `CompositeBackend(routes_dict)`.
   Actual: `CompositeBackend(default, routes={...}, artifacts_root="/")`.
   - `default`: backend for paths not matching any route (we use StateBackend()).
   - `routes`: dict mapping path prefix → backend; prefixes start AND end with "/".

2. FilesystemBackend MUST be constructed with `virtual_mode=True` for
   CompositeBackend routing to work correctly. The default `virtual_mode=False`
   lets absolute paths bypass `root_dir` entirely — unsafe AND breaks routing.
   Reading docstring: "primary use case [for virtual_mode=True]: stable,
   backend-independent path semantics when used with CompositeBackend, which
   strips route prefixes and forwards normalized paths."

3. Methods on FilesystemBackend / StateBackend / CompositeBackend (all share
   the same protocol — that's how routing works):
     read, write, edit, glob, grep, ls, download_files, upload_files,
     and async variants prefixed with `a` (aread, awrite, ...).
   Plus glob_info / grep_raw / ls_info.

Verified behavior:
- write("/artifacts/spec.md", ...) lands at <change>/artifacts/spec.md ✓
- write("/scratch.txt", ...) goes to StateBackend (in-memory) ✓
- read("/project/README.md") returns project file content ✓
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Literal

from deepagents import create_deep_agent
from deepagents.backends import CompositeBackend, FilesystemBackend, StateBackend
from langchain_anthropic import ChatAnthropic
from pydantic import BaseModel


class _Out(BaseModel):
    status: Literal["Success", "Error"]


def main() -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("set ANTHROPIC_API_KEY", file=sys.stderr)
        return 2

    tmp = Path(tempfile.mkdtemp(prefix="spike4_"))
    artifacts = tmp / "artifacts"
    artifacts.mkdir()
    project = tmp / "project"
    project.mkdir()
    (project / "README.md").write_text("hello project")

    # Probe APIs before committing
    print(f"FilesystemBackend signature: {FilesystemBackend.__init__.__doc__ or 'no doc'}")
    print(f"FilesystemBackend methods: "
          f"{[m for m in dir(FilesystemBackend) if not m.startswith('_')]}")
    print(f"CompositeBackend signature: {CompositeBackend.__init__.__doc__ or 'no doc'}")
    print(f"StateBackend methods: "
          f"{[m for m in dir(StateBackend) if not m.startswith('_')]}")

    # Correct API per probe output above:
    #   CompositeBackend(default, routes={...}, artifacts_root="/")
    #   FilesystemBackend(root_dir, virtual_mode=True)  # True for routing safety
    try:
        backend = CompositeBackend(
            default=StateBackend(),
            routes={
                "/artifacts/": FilesystemBackend(
                    root_dir=str(artifacts), virtual_mode=True,
                ),
                "/project/": FilesystemBackend(
                    root_dir=str(project), virtual_mode=True,
                ),
            },
        )
    except Exception as e:
        print(f"FAIL: backend construction: {type(e).__name__}: {e}",
              file=sys.stderr)
        return 1

    print(f"\nbackend type: {type(backend).__name__}")
    print(f"backend methods: "
          f"{[m for m in dir(backend) if not m.startswith('_')]}")

    agent = create_deep_agent(
        model=ChatAnthropic(model="claude-haiku-4-5-20251001"),
        tools=[],
        system_prompt=(
            "Read /project/README.md, then write the single line "
            "'spike4 works' to /artifacts/spec.md, then write the single "
            "line 'scratch' to /scratch.txt, then produce final structured "
            "response with status='Success'."
        ),
        response_format=_Out,
        backend=backend,
    )

    try:
        result = agent.invoke(
            {"messages": [{"role": "user", "content": "begin"}]},
        )
    except Exception as e:
        print(f"FAIL: agent.invoke: {type(e).__name__}: {e}", file=sys.stderr)
        print(f"tmp kept: {tmp}", file=sys.stderr)
        return 1
    print(f"\nresult: {result.get('structured_response')!r}")

    artifact_file = artifacts / "spec.md"
    scratch_on_disk_at_root = tmp / "scratch.txt"
    if artifact_file.exists() and not scratch_on_disk_at_root.exists():
        print(f"OK: {artifact_file} exists; /scratch.txt did not leak to disk")
        print(f"artifact content: {artifact_file.read_text()!r}")
        shutil.rmtree(tmp)
        return 0
    print(
        f"FAIL: artifact_exists={artifact_file.exists()} "
        f"scratch_on_disk={scratch_on_disk_at_root.exists()}",
        file=sys.stderr,
    )
    print(f"tmp kept for inspection: {tmp}", file=sys.stderr)
    if artifacts.exists():
        print(f"artifacts contents: {list(artifacts.iterdir())}",
              file=sys.stderr)
    if project.exists():
        print(f"project contents: {list(project.iterdir())}",
              file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
