"""数据目录：存放 laws.db（目录）、laws_content.db（正文）和进程锁。

默认是仓库根目录下的 data/（不进版本库）；可用环境变量 LAW_CRAWL_DATA_DIR 指到别处。
"""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.environ.get("LAW_CRAWL_DATA_DIR") or REPO_ROOT / "data").resolve()
