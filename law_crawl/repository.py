"""法律法规数据存取。

- laws.db：法规目录（标题、类别、制定机关、地区、日期、时效性、来源），由 law_crawl.sync 生成；
- laws_content.db：法规正文（zlib 压缩的段落文本），由 law_crawl.sync 批量取回。

两个文件都在数据目录（law_crawl.paths.DATA_DIR）下，由流水线发布到仓库的 Release，不进版本库。
"""

from __future__ import annotations

import datetime as dt
import re
import sqlite3
import unicodedata
import zlib
from collections.abc import Iterable
from contextlib import closing
from pathlib import Path

from law_crawl.paths import DATA_DIR

LAWS_DIR = DATA_DIR
METADATA_DB_PATH = LAWS_DIR / "laws.db"
CONTENT_DB_PATH = LAWS_DIR / "laws_content.db"
STATUS_EFFECTIVE = "有效"
STATUS_PENDING = "尚未生效"
STATUS_AMENDED = "已修改"
STATUS_REPEALED = "已废止"
CURRENT_STATUSES = (STATUS_EFFECTIVE, STATUS_PENDING)
STATUS_SCOPE_CURRENT = "current"
STATUS_SCOPE_ALL = "all"
PARAGRAPH_SEPARATOR = "\n"
CATEGORY_ORDER = ("宪法", "法律", "行政法规", "监察法规", "司法解释", "部门规章", "规范性文件", "地方法规")
CATEGORY_RANK_SQL = (
    "CASE top_category "
    + " ".join(f"WHEN '{name}' THEN {rank}" for rank, name in enumerate(CATEGORY_ORDER))
    + f" ELSE {len(CATEGORY_ORDER)} END"
)
PUNCTUATION_PATTERN = re.compile(r"[\s《》〈〉（）()【】\[\]“”\"'‘’、，,。.；;：:！!？?／/\\\-—_·]+")

METADATA_SCHEMA = """
CREATE TABLE laws (
    id             TEXT PRIMARY KEY,
    title          TEXT NOT NULL,
    title_key      TEXT NOT NULL,
    top_category   TEXT NOT NULL,
    category       TEXT NOT NULL,
    issuer         TEXT NOT NULL DEFAULT '',
    region         TEXT NOT NULL DEFAULT '',
    publish_date   TEXT NOT NULL DEFAULT '',
    effective_date TEXT NOT NULL DEFAULT '',
    status         TEXT NOT NULL DEFAULT '',
    is_listed      INTEGER NOT NULL DEFAULT 1,
    source         TEXT NOT NULL DEFAULT 'flk',
    source_url     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX idx_laws_category ON laws(top_category, region);
CREATE INDEX idx_laws_status ON laws(status);
CREATE INDEX idx_laws_publish_date ON laws(publish_date);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '');
CREATE TABLE law_changes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    law_id      TEXT NOT NULL,
    title       TEXT NOT NULL,
    change_type TEXT NOT NULL,
    old_value   TEXT NOT NULL DEFAULT '',
    new_value   TEXT NOT NULL DEFAULT '',
    detected_on TEXT NOT NULL
);
CREATE INDEX idx_law_changes_detected_on ON law_changes(detected_on);
"""
LAW_COLUMNS = ("id", "title", "title_key", "top_category", "category", "issuer", "region",
               "publish_date", "effective_date", "status", "is_listed", "source", "source_url")
# 列的默认值：写库时法规字典缺这些键、或读旧版库缺这些列时使用
LAW_COLUMN_DEFAULTS = {"is_listed": 1, "source": "flk", "source_url": ""}
# 来源：flk 为国家法律法规数据库，其余为各部门官网的规章库（见 ministry_rules.py）
SOURCE_FLK = "flk"
CHANGE_COLUMNS = ("law_id", "title", "change_type", "old_value", "new_value", "detected_on")
# 变更类型：官方库新增、时效性变化、官方库不再收录、重新收录
CHANGE_ADDED = "added"
CHANGE_STATUS = "status"
CHANGE_REMOVED = "removed"
CHANGE_RESTORED = "restored"


def title_key(text: str) -> str:
    """标题检索用归一化：全角转半角、去掉空白和标点、转小写。"""
    return PUNCTUATION_PATTERN.sub("", unicodedata.normalize("NFKC", text or "")).lower()


def create_metadata_db(
    path: Path, laws: Iterable[dict], meta: dict[str, str], changes: Iterable[dict] = ()
) -> None:
    """新建元数据库并写入全部法规、说明信息与变更记录；path 已存在时先删除。

    法规字典缺 is_listed、source、source_url 时取 LAW_COLUMN_DEFAULTS 的默认值。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    with closing(sqlite3.connect(str(path))) as connection, connection:
        connection.executescript(METADATA_SCHEMA)
        connection.executemany(
            f"INSERT INTO laws ({', '.join(LAW_COLUMNS)}) VALUES ({', '.join('?' * len(LAW_COLUMNS))})",
            (
                [law.get(column, LAW_COLUMN_DEFAULTS[column]) if column in LAW_COLUMN_DEFAULTS else law[column]
                 for column in LAW_COLUMNS]
                for law in laws
            ),
        )
        connection.executemany("INSERT INTO meta VALUES (?, ?)", meta.items())
        connection.executemany(
            f"INSERT INTO law_changes ({', '.join(CHANGE_COLUMNS)}) VALUES ({', '.join('?' * len(CHANGE_COLUMNS))})",
            ([change[column] for column in CHANGE_COLUMNS] for change in changes),
        )


def read_all_laws(path: Path) -> list[dict]:
    """元数据库中的全部法规（列同 LAW_COLUMNS）；旧版库缺的列取 LAW_COLUMN_DEFAULTS。库不存在时返回空列表。"""
    if not path.exists():
        return []
    with closing(connect_metadata(path)) as connection:
        available = {row["name"] for row in connection.execute("PRAGMA table_info(laws)")}
        rows = connection.execute(f"SELECT {', '.join(c for c in LAW_COLUMNS if c in available)} FROM laws")
        return [{**LAW_COLUMN_DEFAULTS, **dict(row)} for row in rows]


def read_all_changes(path: Path) -> list[dict]:
    """元数据库中的全部变更记录（按发现先后）；库或表不存在时返回空列表。"""
    if not path.exists():
        return []
    with closing(connect_metadata(path)) as connection:
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE name = 'law_changes'").fetchone():
            return []
        rows = connection.execute(f"SELECT {', '.join(CHANGE_COLUMNS)} FROM law_changes ORDER BY id")
        return [dict(row) for row in rows]


def recent_changes(connection: sqlite3.Connection, limit: int) -> list[dict]:
    """最近发现的变更，新的在前。"""
    rows = connection.execute(
        f"SELECT {', '.join(CHANGE_COLUMNS)} FROM law_changes ORDER BY id DESC LIMIT ?", (limit,)
    )
    return [dict(row) for row in rows]


def connect_metadata(path: Path | None = None) -> sqlite3.Connection:
    """以只读方式打开元数据库（默认 METADATA_DB_PATH）。"""
    connection = sqlite3.connect(f"file:{path or METADATA_DB_PATH}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def build_filters(
    query: str, category: str | None, region: str | None, status_scope: str
) -> tuple[str, list[str]]:
    """检索条件的 WHERE 子句与参数；空格分隔的多个关键词须同时出现在标题中。"""
    clauses, params = [], []
    for keyword in (title_key(part) for part in (query or "").split()):
        if keyword:
            clauses.append("title_key LIKE ? ESCAPE '\\'")
            escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            params.append(f"%{escaped}%")
    if category:
        clauses.append("top_category = ?")
        params.append(category)
    if region:
        clauses.append("region = ?")
        params.append(region)
    if status_scope != STATUS_SCOPE_ALL:
        # 现行范围：时效性为有效或尚未生效，且官方库仍在收录
        clauses.append(f"status IN ({', '.join('?' * len(CURRENT_STATUSES))}) AND is_listed = 1")
        params.extend(CURRENT_STATUSES)
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def search_laws(
    connection: sqlite3.Connection,
    query: str,
    category: str | None,
    region: str | None,
    status_scope: str,
    page: int,
    page_size: int,
) -> dict:
    where, params = build_filters(query, category, region, status_scope)
    total = connection.execute(f"SELECT COUNT(*) FROM laws{where}", params).fetchone()[0]
    # 有关键词时先按效力层级（法律在前、地方法规在后），同层级标题短的在前；否则按公布日期倒序
    order = f"{CATEGORY_RANK_SQL}, LENGTH(title), publish_date DESC" if (query or "").strip() else "publish_date DESC, title"
    rows = connection.execute(
        f"SELECT id, title, top_category, category, issuer, region, publish_date, effective_date, status, is_listed, source "
        f"FROM laws{where} ORDER BY {order} LIMIT ? OFFSET ?",
        [*params, page_size, (page - 1) * page_size],
    ).fetchall()
    return {"items": [dict(row) for row in rows], "total": total, "page": page, "page_size": page_size}


def count_facets(connection: sqlite3.Connection, query: str, category: str | None, status_scope: str) -> dict:
    """各类别的条数，以及所选类别下各地区的条数（都受关键词和时效范围限制）。"""
    where, params = build_filters(query, None, None, status_scope)
    categories = connection.execute(
        f"SELECT top_category AS name, COUNT(*) AS count FROM laws{where} GROUP BY top_category ORDER BY count DESC",
        params,
    ).fetchall()
    where, params = build_filters(query, category, None, status_scope)
    region_clause = (where + " AND " if where else " WHERE ") + "region != ''"
    regions = connection.execute(
        f"SELECT region AS name, COUNT(*) AS count FROM laws{region_clause} GROUP BY region ORDER BY count DESC",
        params,
    ).fetchall()
    return {"categories": [dict(row) for row in categories], "regions": [dict(row) for row in regions]}


def get_law(connection: sqlite3.Connection, law_id: str) -> dict | None:
    row = connection.execute(
        "SELECT id, title, top_category, category, issuer, region, publish_date, effective_date, status, is_listed, "
        "source, source_url FROM laws WHERE id = ?",
        (law_id,),
    ).fetchone()
    return dict(row) if row else None


def read_meta(connection: sqlite3.Connection) -> dict[str, str]:
    return {row["key"]: row["value"] for row in connection.execute("SELECT key, value FROM meta")}


def count_by_status(connection: sqlite3.Connection) -> list[dict]:
    rows = connection.execute("SELECT status AS name, COUNT(*) AS count FROM laws GROUP BY status ORDER BY count DESC")
    return [dict(row) for row in rows]


class ContentCache:
    """法规正文缓存：每部法规一行，正文为 zlib 压缩的段落文本（换行分隔）。"""

    def __init__(self, path: Path = CONTENT_DB_PATH) -> None:
        self.path = path

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self.path))
        connection.execute(
            "CREATE TABLE IF NOT EXISTS contents ("
            "id TEXT PRIMARY KEY, body BLOB NOT NULL, paragraph_count INTEGER NOT NULL, fetched_at TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS unreadable (id TEXT PRIMARY KEY, reason TEXT NOT NULL, recorded_at TEXT NOT NULL)"
        )
        return connection

    def get(self, law_id: str) -> dict | None:
        with closing(self.connect()) as connection:
            row = connection.execute("SELECT body, fetched_at FROM contents WHERE id = ?", (law_id,)).fetchone()
        if not row:
            return None
        text = zlib.decompress(row[0]).decode("utf-8")
        return {"paragraphs": text.split(PARAGRAPH_SEPARATOR) if text else [], "fetched_at": row[1]}

    def put(self, law_id: str, paragraphs: list[str]) -> str:
        fetched_at = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
        body = zlib.compress(PARAGRAPH_SEPARATOR.join(paragraphs).encode("utf-8"))
        with closing(self.connect()) as connection, connection:
            connection.execute(
                "INSERT OR REPLACE INTO contents VALUES (?, ?, ?, ?)", (law_id, body, len(paragraphs), fetched_at)
            )
        return fetched_at

    def cached_ids(self) -> set[str]:
        with closing(self.connect()) as connection:
            return {row[0] for row in connection.execute("SELECT id FROM contents")}

    def mark_unreadable(self, law_id: str, reason: str) -> None:
        """记下正文文件无法解析的法规（如官方只提供旧版 doc），批量预取时不再重复下载。"""
        recorded_at = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
        with closing(self.connect()) as connection, connection:
            connection.execute("INSERT OR REPLACE INTO unreadable VALUES (?, ?, ?)", (law_id, reason, recorded_at))

    def unreadable_ids(self) -> set[str]:
        with closing(self.connect()) as connection:
            return {row[0] for row in connection.execute("SELECT id FROM unreadable")}
