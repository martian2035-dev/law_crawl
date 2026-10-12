"""Reading the collected data: title search, category counts and law texts (fetched from the source on demand and cached)."""

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
# Only one text is fetched from the national database at a time, at least MIN_FETCH_INTERVAL_SECONDS apart
_fetch_lock = threading.Lock()
_last_fetch_at = 0.0


def wait_for_fetch_slot() -> None:
    """Call while holding _fetch_lock: wait if the previous fetch was less than the minimum interval ago."""
    global _last_fetch_at
    remaining = MIN_FETCH_INTERVAL_SECONDS - (time.monotonic() - _last_fetch_at)
    if remaining > 0:
        time.sleep(remaining)
    _last_fetch_at = time.monotonic()


class LawNotFound(Exception):
    """No law has this id."""


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
    """Search laws by title.

    Space-separated keywords must all appear in the title; by default only laws in force or not yet in force
    are searched.
    """
    with closing(repository.connect_metadata()) as connection:
        result = repository.search_laws(connection, query, category, region, status_scope, page, page_size)
        facets = repository.count_facets(connection, query, category, status_scope)
    return {**result, "facets": facets}


def get_info() -> dict:
    """Data source, snapshot date and row count per validity status."""
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
    """Recently detected catalogue changes (added, status changed, delisted, relisted), newest first."""
    with closing(repository.connect_metadata()) as connection:
        return repository.recent_changes(connection, limit)


def official_url_of(law: dict) -> str:
    """Official address of a law: its detail page in the national database, or the rule's page on the ministry site."""
    if law["source"] == repository.SOURCE_FLK:
        return flk_client.official_url(law["id"], law["title"])
    return law["source_url"]


def source_name_of(law: dict) -> str:
    """Name of the source, in Chinese like the other data values ("<ministry> official rule library")."""
    return flk_client.SOURCE_NAME if law["source"] == repository.SOURCE_FLK else f"{law['issuer']}官网规章库"


def load_paragraphs(law_id: str, cache: ContentCache, fetch_docx: DocxFetcher) -> dict:
    """Text paragraphs: from the cache, or fetched from the source as docx, parsed and cached."""
    cached = cache.get(law_id)
    if cached is not None:
        return cached
    with _fetch_lock:
        cached = cache.get(law_id)  # another request may have fetched it while this one waited for the lock
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
    """Details and text of a law.

    When the text cannot be had (no docx at the source, network failure, rule text not collected) content is
    None and content_error says why; metadata and the official link are returned all the same.
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
        # Ministry rule texts are fetched during sync; nothing is requested when reading
        loaded = cache.get(law_id) if cache.path.exists() else None
        if loaded is None:
            return {**document, "content_error": "the text of this rule has not been collected yet"}
    else:
        try:
            loaded = load_paragraphs(law_id, cache, fetch_docx or flk_client.fetch_docx)
        except (flk_client.LawSourceUnavailable, ValueError) as error:
            return {**document, "content_error": str(error) or "the text is unavailable right now"}
    return {
        **document,
        "content": {"blocks": classify_blocks(loaded["paragraphs"]), "fetched_at": loaded["fetched_at"]},
    }
