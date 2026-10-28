"""Launcher for the local dashboard.

The dashboard moved into the ``dashboard`` package (see ``dashboard/ARCHITECTURE.md``).
``python tools/dashboard.py`` and ``python -m dashboard`` are the same thing; every
option is documented in ``python -m dashboard --help``.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))  # runnable from any working directory

from dashboard.cli import main  # noqa: E402

if __name__ == "__main__":
    main()
