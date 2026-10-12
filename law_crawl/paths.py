"""Data directory: holds laws.db (catalogue), laws_content.db (texts) and the process locks.

Defaults to data/ in the repository root (not committed); set LAW_CRAWL_DATA_DIR to put it elsewhere.
"""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.environ.get("LAW_CRAWL_DATA_DIR") or REPO_ROOT / "data").resolve()
