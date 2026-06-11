from __future__ import annotations

import sys
from pathlib import Path

# Put the driver module dir on sys.path so the contrib tests can import it
# without an editable install (these tests ship out-of-core and run standalone).
sys.path.insert(0, str(Path(__file__).parent))
