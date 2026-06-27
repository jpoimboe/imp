import sys
from pathlib import Path

# In the sandbox worktree, pip can't write to the read-only home directory, so
# tree-sitter-language-pack lands in .packages/ instead of site-packages.
_local_packages = Path(__file__).parent / ".packages"
if _local_packages.is_dir():
    sys.path.insert(0, str(_local_packages))
