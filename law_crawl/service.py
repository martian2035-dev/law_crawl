"""法律法规查询：标题检索、分类统计、法规正文（按需从官方取回并缓存）。"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable
from contextlib import closing

import httpx

from law_crawl import flk_client, repository
from law_crawl.docx_text import classify_blocks, extract_paragraphs
from law_crawl.repository import ContentCache

LAW_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
DocxFetcher = Callable[[httpx.Client, str], bytes]
MIN_FETCH_INTERVAL_SECONDS = 1.0
# 同一时刻只向官方数据库取一份正文，两次取数之间至少间隔 MIN_FETCH_INTERVAL_SECONDS
_fetch_lock = threading.Lock()
_last_fetch_at = 0.0


def wait_for_fetch_slot() -> None:
    """在持有 _fetch_lock 时调用：距上次取数不足最小间隔则等待。"""
    global _last_fetch_at
    remaining = MIN_FETCH_INTERVAL_SECONDS - (time.monotonic() - _last_fetch_at)
    if remaining > 0:
        time.sleep(remaining)
    _last_fetch_at = time.monotonic()


class LawNotFound(Exception):
    """法规编号不存在。"""


def database_ready() -> bool:
    return repository.METADATA_DB_PATH.exists()


def search(
    query: str = "",
    category: str | None = None,
    region: str | None = None,
    status_scope: str = repository.STATUS_SCOPE_CURRENT,
    page: int = 1,
    page_size: int = 20,
) -> dict:
    """
    按标题检索法规
    空格分隔的多个关键词须同时出现在标题中；默认只查现行（有效、尚未生效）法规
    """
    with closing(repository.connect_metadata()) as connection:
        result = repository.search_laws(connection, query, category, region, status_scope, page, page_size)
        facets = repository.count_facets(connection, query, category, status_scope)
    return {**result, "facets": facets}


def get_info() -> dict:
    """数据来源、快照日期与各时效状态的条数"""
    with closing(repository.connect_metadata()) as connection:
        meta = repository.read_meta(connection)
        statuses = repository.count_by_status(connection)
    return {
        "source": flk_client.SOURCE_NAME,
        "snapshot_date": meta.get("snapshot_date", ""),
        "total": int(meta.get("count_total", "0")),
        "statuses": statuses,
    }


def get_recent_changes(limit: int = 50) -> list[dict]:
    """最近发现的目录变更（新增、时效性变化、不再收录、重新收录），新的在前"""
    with closing(repository.connect_metadata()) as connection:
        return repository.recent_changes(connection, limit)


def official_url_of(law: dict) -> str:
    """法规的官方原文地址：国家法律法规数据库的详情页，或部门官网上的规章页面。"""
    if law["source"] == repository.SOURCE_FLK:
        return flk_client.official_url(law["id"], law["title"])
    return law["source_url"]


def source_name_of(law: dict) -> str:
    return flk_client.SOURCE_NAME if law["source"] == repository.SOURCE_FLK else f"{law['issuer']}官网规章库"


def load_paragraphs(law_id: str, cache: ContentCache, fetch_docx: DocxFetcher) -> dict:
    """正文段落：先读缓存，没有时从官方取回 docx 解析后写入缓存。"""
    cached = cache.get(law_id)
    if cached is not None:
        return cached
    with _fetch_lock:
        cached = cache.get(law_id)  # 等锁期间可能已被其他请求取回
        if cached is not None:
            return cached
        wait_for_fetch_slot()
        with flk_client.new_client() as client:
            paragraphs = extract_paragraphs(fetch_docx(client, law_id))
        return {"paragraphs": paragraphs, "fetched_at": cache.put(law_id, paragraphs)}


def get_document(
    law_id: str,
    cache: ContentCache | None = None,
    fetch_docx: DocxFetcher | None = None,
) -> dict:
    """
    法规详情与正文
    正文取不到时（官方无 docx、网络故障、规章正文未收录）content 为 None 并给出原因，元数据和官方链接照常返回
    """
    if not LAW_ID_PATTERN.fullmatch(law_id):
        raise LawNotFound(law_id)
    with closing(repository.connect_metadata()) as connection:
        law = repository.get_law(connection, law_id)
    if law is None:
        raise LawNotFound(law_id)
    document = {
        **law,
        "official_url": official_url_of(law),
        "source": source_name_of(law),
        "content": None,
        "content_error": "",
    }
    cache = cache or ContentCache()
    if law["source"] != repository.SOURCE_FLK:
        # 部门规章的正文在同步时取回，查看时不再联网
        loaded = cache.get(law_id) if cache.path.exists() else None
        if loaded is None:
            return {**document, "content_error": "这部规章的正文尚未收录"}
    else:
        try:
            loaded = load_paragraphs(law_id, cache, fetch_docx or flk_client.fetch_docx)
        except (flk_client.LawSourceUnavailable, ValueError) as error:
            return {**document, "content_error": str(error) or "正文暂时无法获取"}
    return {
        **document,
        "content": {"blocks": classify_blocks(loaded["paragraphs"]), "fetched_at": loaded["fetched_at"]},
    }
