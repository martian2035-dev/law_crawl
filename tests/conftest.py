"""Point the data directory at a temporary directory so tests never touch data/ in the repository."""

import os
import tempfile

os.environ["LAW_CRAWL_DATA_DIR"] = tempfile.mkdtemp(prefix="law_crawl_test_")
