"""imp CLI entrypoint.

Plan 1 wires the minimal command set: new, resume, log, status. No streaming,
no signal handling, no spinner — that lands in Plan 3.
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args:
        print("usage: imp {new,resume,log,status} ...", file=sys.stderr)
        return 2
    # Command dispatch is filled in by later tasks.
    print(f"imp: unknown command {args[0]!r}", file=sys.stderr)
    return 2
