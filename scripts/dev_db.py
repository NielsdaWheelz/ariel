#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import sys


_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SRC_DIR = _PROJECT_ROOT / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from ariel.dev_db import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
