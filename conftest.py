import os
import sys
from pathlib import Path

# In the sandbox worktree, pip can't write to the read-only home directory, so
# tree-sitter-language-pack lands in .packages/ instead of site-packages. Put it
# on sys.path for in-process imports, and on PYTHONPATH so subprocesses spawned
# by the tests (cold imports, the `imp` CLI) resolve it too. Inert in real CI,
# where .packages/ does not exist.
_local_packages = Path(__file__).parent / ".packages"
if _local_packages.is_dir():
    _pkgs = str(_local_packages)
    if _pkgs not in sys.path:
        sys.path.insert(0, _pkgs)
    _existing = os.environ.get("PYTHONPATH", "")
    _parts = _existing.split(os.pathsep) if _existing else []
    if _pkgs not in _parts:
        os.environ["PYTHONPATH"] = os.pathsep.join([_pkgs, *_parts])
