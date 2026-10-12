"""测试数据目录指向临时目录，不碰仓库里的 data/。"""

import os
import tempfile

os.environ["LAW_CRAWL_DATA_DIR"] = tempfile.mkdtemp(prefix="law_crawl_test_")
