"""Collecting laws, regulations and ministry rules.

    python -m law_crawl update            # scheduled run: catalogue diff + missing texts + ministry rules
    python -m law_crawl metadata          # only sync the national catalogue -> data/laws.db
    python -m law_crawl content --limit 500 --budget-seconds 1800   # only fetch texts -> data/laws_content.db (resumable)
    python -m law_crawl ministries        # only sync the ministry rule libraries
    python -m law_crawl probe             # one request per source, to see whether this network can reach them
    python -m law_crawl manifest          # print the data manifest (counts, snapshot date) for manifest.json

Catalogue (metadata): the National Database of Laws and Regulations (flk.npc.gov.cn) has no "recent changes"
endpoint, and the validity status of an old law can change while its publication date stays the same. So every
run downloads the whole catalogue (about 125 requests) and diffs it against the existing database: additions,
status changes and delistings are each recorded in the law_changes table; a delisted law is kept and flagged
is_listed = 0, never deleted. The result is written to a temporary file and swapped in after validation.

Texts (content): fetches the texts not yet cached, national laws first and laws in force first, and stops at the
count or time limit; the next run picks up where this one stopped. After a streak of failures it pauses for a few
minutes and carries on, giving up only if several pauses do not help; a browser challenge stops it immediately.
Files that cannot be parsed (for example when only a legacy .doc is offered) are recorded and not downloaded again.

Ministry rules (ministries): the national database does not carry ministry rules, so they are collected ministry
by ministry from the rule library on each official site (see ministry_rules.py). They live in the same database
as the laws above; the source column tells them apart.

Rate limiting: the national database answers with a browser challenge page and refuses service when it is hit
too often. The crawler stops as soon as it sees that page (exit code 2), allows one process per source at a
time, and waits between requests (see the constants below). Do not raise the request rate or work around the
challenge.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import logging
import os
import re
import sqlite3
import sys
import time
from contextlib import closing, contextmanager
from functools import lru_cache
from pathlib import Path

import httpx

from law_crawl import flk_client, ministry_rules, repository
from law_crawl.docx_text import REASON_NOT_DOCX, LawTextError, extract_paragraphs
from law_crawl.html_text import extract_article_paragraphs
from law_crawl.repository import ContentCache

REGION_DATA_PATH = Path(__file__).with_name("china_region_centers.json")

logger = logging.getLogger("laws_sync")

# Delay between requests: the national database shows a browser challenge and refuses service when it is hit
# too often, so stay slow
REQUEST_INTERVAL_SECONDS = 1.0
CONTENT_INTERVAL_SECONDS = 2.0
# Process locks: one for the national database and one for the ministry sites, so the two can run side by side;
# a third, short-lived lock is held while the catalogue database is rewritten
LOCK_NATIONAL = "laws_sync.lock"
LOCK_MINISTRIES = "laws_sync_ministries.lock"
LOCK_METADATA_WRITE = "laws_metadata_write.lock"
EXIT_BLOCKED = 2
MAX_CONSECUTIVE_FAILURES = 10
# A streak of failures is usually a short network outage: pause, then carry on; give up only after several pauses
FAILURE_PAUSE_SECONDS = 300
MAX_FAILURE_PAUSES = 6
PROGRESS_EVERY = 200
DEFAULT_UPDATE_CONTENT_LIMIT = 100000
DEFAULT_UPDATE_BUDGET_SECONDS = 6 * 3600
MIN_EXPECTED_LAWS = 20000
# When more than this share of the laws disappears from the catalogue in one run, treat it as a source fault and
# keep the existing database
MAX_REMOVED_RATIO = 0.05
# Names used by the national database (issuer group, categories): data values, kept in Chinese
LOCAL_ISSUER_GROUP = "地方人大及其常委会"
LOCAL_CATEGORY = "地方法规"
OTHER_CATEGORY = "其他"
AUTONOMOUS_COUNTY_PATTERN = re.compile(r"自治[县旗]$")
STATUS_BY_CODE = {
    4: repository.STATUS_PENDING,
    3: repository.STATUS_EFFECTIVE,
    2: repository.STATUS_AMENDED,
    1: repository.STATUS_REPEALED,
}
# Category order when fetching texts in bulk
CATEGORY_PRIORITY = ("宪法", "法律", "行政法规", "监察法规", "司法解释", "部门规章", "规范性文件", "地方法规")


class AlreadyRunning(Exception):
    """Another sync process holds the lock."""


def category_names(category_tree: dict) -> dict[int, tuple[str, str]]:
    """Category code -> (top-level category, name of this category)."""
    names: dict[int, tuple[str, str]] = {}

    def walk(node: dict, top: str | None) -> None:
        for child in node.get("children") or []:
            top_name = top or child["name"]
            names[child["codeId"]] = (top_name, child["name"])
            walk(child, top_name)

    walk(category_tree, None)
    return names


def province_issuers(issuer_tree: dict) -> list[tuple[int, str]]:
    """Province-level local issuers: (code, region name)."""
    for group in issuer_tree.get("children") or []:
        if group["name"] == LOCAL_ISSUER_GROUP:
            return [(child["codeId"], child["name"]) for child in group.get("children") or []]
    return []


@lru_cache(maxsize=1)
def load_regions() -> dict:
    """Administrative division names (province -> city -> county), used to tell the region of a local regulation
    from the name of its issuer."""
    return json.loads(REGION_DATA_PATH.read_text(encoding="utf-8"))


def place_index(province_names: list[str]) -> list[tuple[str, str]]:
    """(place name, region name) pairs, longest place name first: provinces, prefecture-level cities, autonomous
    prefectures and autonomous counties (banners) -> the region name used by the national database.

    Filtering the database by province-level issuer misses some prefecture-level cities; for those laws the
    region is taken from the beginning of the issuer's name.
    """
    places: dict[str, str] = {}
    for province in load_regions()["provinces"]:
        region = next((name for name in province_names if province["name"].startswith(name)), None)
        if region is None:
            continue
        places[province["name"]] = places[region] = region
        for city in province.get("children") or []:
            places.setdefault(city["name"], region)
            for district in city.get("children") or []:
                if AUTONOMOUS_COUNTY_PATTERN.search(district["name"]):
                    places.setdefault(district["name"], region)
    return sorted(places.items(), key=lambda item: -len(item[0]))


def region_by_issuer(issuer: str, places: list[tuple[str, str]]) -> str:
    return next((region for place, region in places if issuer.startswith(place)), "")


def fetch_all_rows(client: httpx.Client, issuer_codes: list[int] | None = None) -> list[dict]:
    """Page through the list and return every row matching the filter."""
    rows: list[dict] = []
    page = 1
    while True:
        payload = flk_client.fetch_list_page(client, page, issuer_codes)
        rows.extend(payload["rows"])
        if not payload["rows"] or len(rows) >= payload["total"]:
            return rows
        page += 1
        time.sleep(REQUEST_INTERVAL_SECONDS)


def to_law(row: dict, categories: dict[int, tuple[str, str]], region: str) -> dict:
    """List row -> catalogue row. A category code missing from the category tree falls back to the local-regulation
    category when the law has a region, and to "other" otherwise."""
    fallback = row.get("flxz") or (LOCAL_CATEGORY if region else OTHER_CATEGORY)
    top_category, category = categories.get(row.get("flfgCodeId"), (fallback, fallback))
    title = (row.get("title") or "").strip()
    return {
        "id": row["bbbs"],
        "title": title,
        "title_key": repository.title_key(title),
        "top_category": top_category,
        "category": category,
        "issuer": row.get("zdjgName") or "",
        "region": region,
        "publish_date": row.get("gbrq") or "",
        "effective_date": row.get("sxrq") or "",
        "status": STATUS_BY_CODE.get(row.get("sxx"), ""),
    }


def collect_laws(client: httpx.Client) -> list[dict]:
    """Fetch the metadata of every law and work out the region of local regulations."""
    enums = flk_client.fetch_enums(client)
    categories = category_names(enums["flfgfl"])
    rows = fetch_all_rows(client)
    logger.info("Fetched %d catalogue rows; assigning regions", len(rows))
    regions: dict[str, str] = {}
    for code, name in province_issuers(enums["zdjgfl"]):
        province_rows = fetch_all_rows(client, [code])
        regions.update({row["bbbs"]: name for row in province_rows})
        logger.info("  %s: %d", name, len(province_rows))
        time.sleep(REQUEST_INTERVAL_SECONDS)
    places = place_index([name for _, name in province_issuers(enums["zdjgfl"])])
    unique_rows = {row["bbbs"]: row for row in rows if row.get("bbbs") and row.get("title")}
    return [
        to_law(row, categories, regions.get(law_id) or region_by_issuer(row.get("zdjgName") or "", places))
        for law_id, row in unique_rows.items()
    ]


def merge_with_previous(
    previous: list[dict], current: list[dict], today: str, source: str = repository.SOURCE_FLK
) -> tuple[list[dict], list[dict]]:
    """Merge the catalogue just fetched from one source into the existing database and return
    (all laws after the merge, the changes found in this run).

    Only laws of the same source are compared; those of other sources are kept untouched. When the source has
    no rows yet (first import) no per-law "added" change is recorded. Laws the source no longer lists are kept
    and flagged is_listed = 0.
    """
    previous = [{**repository.LAW_COLUMN_DEFAULTS, **law} for law in previous]
    others = [law for law in previous if law["source"] != source]
    previous = [law for law in previous if law["source"] == source]
    current = [{**law, "source": source} for law in current]
    previous_by_id = {law["id"]: law for law in previous}
    current_ids = {law["id"] for law in current}

    def change(law: dict, change_type: str, old_value: str = "", new_value: str = "") -> dict:
        return {"law_id": law["id"], "title": law["title"], "change_type": change_type,
                "old_value": old_value, "new_value": new_value, "detected_on": today}

    changes: list[dict] = []
    for law in current:
        old = previous_by_id.get(law["id"])
        if old is None:
            if previous:
                changes.append(change(law, repository.CHANGE_ADDED, new_value=law["status"]))
            continue
        if not old["is_listed"]:
            changes.append(change(law, repository.CHANGE_RESTORED, new_value=law["status"]))
        if old["status"] != law["status"]:
            changes.append(change(law, repository.CHANGE_STATUS, old["status"], law["status"]))
    carried = []
    for law in previous:
        if law["id"] in current_ids:
            continue
        carried.append({**law, "is_listed": 0})
        if law["is_listed"]:
            changes.append(change(law, repository.CHANGE_REMOVED, old_value=law["status"]))
    return [*others, *({**law, "is_listed": 1} for law in current), *carried], changes


def repository_meta(path) -> dict[str, str]:
    """The meta table of the existing catalogue database; empty when the file does not exist."""
    if not path.exists():
        return {}
    with closing(repository.connect_metadata(path)) as connection:
        return repository.read_meta(connection)


def count_changes(changes: list[dict]) -> dict[str, int]:
    counts = {change_type: 0 for change_type in (
        repository.CHANGE_ADDED, repository.CHANGE_STATUS, repository.CHANGE_REMOVED, repository.CHANGE_RESTORED)}
    for item in changes:
        counts[item["change_type"]] += 1
    return counts


def sync_metadata(today: dt.date | None = None) -> int:
    """Download the whole catalogue and merge it into the existing database; the existing database is kept when
    the source looks faulty (too few rows, or a large share of the laws gone)."""
    with flk_client.new_client() as client:
        current = collect_laws(client)
    if len(current) < MIN_EXPECTED_LAWS:
        logger.error("Only %d rows fetched, fewer than the expected %d; keeping the existing database", len(current), MIN_EXPECTED_LAWS)
        return 1
    with metadata_write_lock():
        target = repository.METADATA_DB_PATH
        previous = repository.read_all_laws(target)
        merged, changes = merge_with_previous(previous, current, (today or dt.date.today()).isoformat())
        counts = count_changes(changes)
        listed_before = sum(1 for law in previous if law["is_listed"] and law["source"] == repository.SOURCE_FLK)
        if listed_before and counts[repository.CHANGE_REMOVED] > listed_before * MAX_REMOVED_RATIO:
            logger.error(
                "%d laws are no longer listed, out of %d currently listed (limit %.0f%%); treating this as a source fault and "
                "keeping the existing database",
                counts[repository.CHANGE_REMOVED], listed_before, MAX_REMOVED_RATIO * 100,
            )
            return 1
        meta = {
            "snapshot_date": (today or dt.date.today()).isoformat(),
            **{key: value for key, value in repository_meta(target).items() if key.startswith("ministry_")},
            "count_total": str(sum(1 for law in merged if law["is_listed"])),
            "count_unlisted": str(sum(1 for law in merged if not law["is_listed"])),
            "source": flk_client.SOURCE_NAME,
        }
        temporary = target.with_suffix(".db.tmp")
        repository.create_metadata_db(temporary, merged, meta, [*repository.read_all_changes(target), *changes])
        os.replace(temporary, target)
    logger.info(
        "Catalogue updated: %d listed, %d added, %d status changes, %d delisted, %d relisted",
        len(current), counts[repository.CHANGE_ADDED], counts[repository.CHANGE_STATUS],
        counts[repository.CHANGE_REMOVED], counts[repository.CHANGE_RESTORED],
    )
    for item in changes[:20]:
        logger.info("  [%s] %s %s → %s", item["change_type"], item["title"], item["old_value"] or "—", item["new_value"] or "—")
    return 0


def pending_content_ids(cache: ContentCache, category: str | None) -> list[str]:
    """Ids of the laws whose text is not cached yet, national laws and laws in force first."""
    with closing(repository.connect_metadata()) as connection:
        rows: list[sqlite3.Row] = connection.execute(
            "SELECT id, top_category, status FROM laws WHERE source = ?" + (" AND top_category = ?" if category else ""),
            (repository.SOURCE_FLK, category) if category else (repository.SOURCE_FLK,),
        ).fetchall()
    cached = cache.cached_ids() | cache.unreadable_ids()

    def priority(row: sqlite3.Row) -> tuple[int, int]:
        order = CATEGORY_PRIORITY.index(row["top_category"]) if row["top_category"] in CATEGORY_PRIORITY else len(CATEGORY_PRIORITY)
        return (order, 0 if row["status"] in repository.CURRENT_STATUSES else 1)

    return [row["id"] for row in sorted(rows, key=priority) if row["id"] not in cached]


def sync_content(limit: int, budget_seconds: float, category: str | None, retry_unreadable: bool = False) -> int:
    cache = ContentCache()
    retry_ids: list[str] = cache.forget_unreadable(REASON_NOT_DOCX) if retry_unreadable else []
    if retry_unreadable:
        # Laws recorded as "not a docx" are mostly legacy docs that could not be converted before LibreOffice was
        # available; fetch them again, ahead of the rest of the queue
        logger.info("Cleared %d unreadable records (not a docx); fetching them again", len(retry_ids))
    pending = pending_content_ids(cache, category)
    first = set(retry_ids)
    pending = [law_id for law_id in pending if law_id in first] + [law_id for law_id in pending if law_id not in first]
    logger.info("%d texts pending; this run fetches at most %d within %d seconds", len(pending), limit, budget_seconds)
    started = time.monotonic()
    fetched = failed = unreadable = consecutive_failures = pauses = 0
    stopped_by_failures = False
    with flk_client.new_client() as client:
        for law_id in pending[:limit]:
            if time.monotonic() - started > budget_seconds:
                break
            try:
                cache.put(law_id, extract_paragraphs(flk_client.fetch_docx(client, law_id)))
                fetched += 1
                consecutive_failures = 0
            except LawTextError as error:
                # The file arrived but is not a docx: record it so it is skipped from now on; not a source failure
                cache.mark_unreadable(law_id, str(error))
                unreadable += 1
                consecutive_failures = 0
            except flk_client.LawSourceBlocked:
                logger.info("Browser challenge after fetching %d texts; stopping", fetched)
                raise
            except flk_client.LawSourceUnavailable as error:
                failed += 1
                consecutive_failures += 1
                logger.warning("  %s: text not fetched: %s", law_id, error)
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    if pauses >= MAX_FAILURE_PAUSES:
                        logger.error("%d failures in a row and no recovery after %d pauses; stopping", consecutive_failures, pauses)
                        stopped_by_failures = True
                        break
                    pauses += 1
                    consecutive_failures = 0
                    logger.warning(
                        "%d failures in a row; pausing %d seconds before continuing (pause %d)",
                        MAX_CONSECUTIVE_FAILURES, FAILURE_PAUSE_SECONDS, pauses,
                    )
                    time.sleep(FAILURE_PAUSE_SECONDS)
            if (fetched + unreadable) % PROGRESS_EVERY == 0 and fetched + unreadable:
                logger.info("  processed %d / %d", fetched + unreadable + failed, min(limit, len(pending)))
            time.sleep(CONTENT_INTERVAL_SECONDS)
    logger.info(
        "This run: %d fetched, %d unreadable, %d failed, %d remaining",
        fetched, unreadable, failed, len(pending) - fetched - unreadable,
    )
    return 1 if stopped_by_failures else 0


def write_ministry_rules(source: ministry_rules.RuleSource, rules: list[dict], today: dt.date) -> dict[str, int]:
    """Merge one ministry's rules into the catalogue database and return the number of changes of each type."""
    target = repository.METADATA_DB_PATH
    with metadata_write_lock():
        merged, changes = merge_with_previous(repository.read_all_laws(target), rules, today.isoformat(), source=source.key)
        meta = {
            **repository_meta(target),
            f"ministry_{source.key}_snapshot": today.isoformat(),
            f"ministry_{source.key}_count": str(len(rules)),
            "count_total": str(sum(1 for law in merged if law["is_listed"])),
            "count_unlisted": str(sum(1 for law in merged if not law["is_listed"])),
        }
        temporary = target.with_suffix(".db.tmp")
        repository.create_metadata_db(temporary, merged, meta, [*repository.read_all_changes(target), *changes])
        os.replace(temporary, target)
    return count_changes(changes)


def fetch_rule_paragraphs(client: httpx.Client, source: ministry_rules.RuleSource, rule: dict) -> list[str]:
    """Paragraphs of a rule's text: from the docx when one is offered, otherwise from the detail page."""
    if rule["docx_url"]:
        return extract_paragraphs(ministry_rules.fetch_rule_docx(client, rule["docx_url"], source))
    return extract_article_paragraphs(ministry_rules.fetch_rule_page(client, rule["text_url"], source))


def fetch_ministry_contents(client: httpx.Client, source: ministry_rules.RuleSource, rules: list[dict], cache: ContentCache) -> int:
    """Fetch the rules whose text is not cached yet and return how many were fetched. Texts hosted outside the
    ministry's own site (links to other sites) are skipped."""
    done = cache.cached_ids() | cache.unreadable_ids()
    fetched = 0
    for rule in rules:
        content_url = rule["docx_url"] or rule["text_url"]
        if rule["id"] in done or not ministry_rules.is_source_host(content_url, source):
            continue
        try:
            cache.put(rule["id"], fetch_rule_paragraphs(client, source, rule))
            fetched += 1
        except LawTextError as error:
            cache.mark_unreadable(rule["id"], str(error))
        except ministry_rules.MinistrySourceUnavailable as error:
            logger.warning("  %s: text not fetched: %s", rule["title"], error)
        time.sleep(ministry_rules.REQUEST_INTERVAL_SECONDS)
    return fetched


def sync_ministries(today: dt.date | None = None) -> int:
    """Sync the catalogue and texts of every ministry rule library; one ministry failing does not stop the others."""
    today = today or dt.date.today()
    if not repository.METADATA_DB_PATH.exists():
        logger.error("The catalogue database does not exist; run `metadata` first")
        return 1
    status = 0
    cache = ContentCache()
    with ministry_rules.new_client() as client:
        for source in ministry_rules.SOURCES:
            try:
                rules = ministry_rules.fetch_rules(client, source, today)
            except ministry_rules.MinistrySourceUnavailable as error:
                logger.error("%s: rule library sync failed: %s", source.key, error)
                status = 1
                continue
            if len(rules) < source.min_expected:
                logger.error(
                    "%s: only %d rules fetched, fewer than the expected %d; not updating",
                    source.key, len(rules), source.min_expected,
                )
                status = 1
                continue
            counts = write_ministry_rules(source, rules, today)
            fetched = fetch_ministry_contents(client, source, rules, cache)
            logger.info(
                "%s: %d rules, %d added, %d status changes, %d delisted; %d texts fetched",
                source.key, len(rules), counts[repository.CHANGE_ADDED], counts[repository.CHANGE_STATUS],
                counts[repository.CHANGE_REMOVED], fetched,
            )
    return status


def sync_ministries_exclusively() -> int:
    """Sync the ministry rules; skipped when another process is already doing so."""
    try:
        with single_instance(LOCK_MINISTRIES):
            return sync_ministries()
    except AlreadyRunning:
        logger.info("Another process is syncing the ministry rules; skipping this step")
        return 0


def sync_national(content_limit: int, budget_seconds: float, retry_unreadable: bool = False) -> int:
    """National database: diff the catalogue, then fetch the missing texts. Returns EXIT_BLOCKED on a browser
    challenge.

    Skipped, and not counted as a failure, when another process is already working on the national database
    (for example a full text download that has not finished).
    """
    try:
        with single_instance(LOCK_NATIONAL):
            if sync_metadata() != 0:
                return 1
            return sync_content(content_limit, budget_seconds, None, retry_unreadable) if content_limit > 0 else 0
    except AlreadyRunning:
        logger.info("Another process is syncing the national database; skipping this step")
        return 0
    except flk_client.LawSourceBlocked as error:
        logger.error("National database sync aborted: %s", error)
        return EXIT_BLOCKED
    except flk_client.LawSourceUnavailable as error:
        logger.error("National database sync aborted: %s", error)
        return 1


def update(content_limit: int, budget_seconds: float, retry_unreadable: bool = False) -> int:
    """Scheduled run: the national database, then the ministry rule libraries.

    The two steps are independent: the second runs even when the first fails, and the first non-zero exit code
    is returned.
    """
    statuses = [sync_national(content_limit, budget_seconds, retry_unreadable), sync_ministries_exclusively()]
    return next((status for status in statuses if status), 0)


PROBE_TIMEOUT_SECONDS = 20


def probe() -> int:
    """Send one request to each source and report whether this network can reach it; 0 when all are reachable."""
    reachable = True
    with flk_client.new_client() as client:
        try:
            flk_client.fetch_enums(client)
            logger.info("reachable    flk (%s)", flk_client.BASE_URL)
        except flk_client.LawSourceUnavailable as error:
            reachable = False
            logger.info("unreachable  flk (%s): %s", flk_client.BASE_URL, error)
    with ministry_rules.new_client() as client:
        for source in ministry_rules.SOURCES:
            try:
                response = client.get(source.list_url, timeout=PROBE_TIMEOUT_SECONDS)
                ok = response.status_code == 200
                detail = f"HTTP {response.status_code}"
            except httpx.HTTPError as error:
                ok, detail = False, type(error).__name__
            reachable = reachable and ok
            logger.info("%s  %s (%s): %s", "reachable  " if ok else "unreachable", source.key, source.list_url, detail)
    return 0 if reachable else 1


def build_manifest() -> dict:
    """Data manifest: catalogue snapshot date, row count per source, number of laws with text, recent changes."""
    path = repository.METADATA_DB_PATH
    if not path.exists():
        return {"laws": 0, "with_content": 0}
    laws = repository.read_all_laws(path)
    cache = ContentCache()
    cached = cache.cached_ids() if cache.path.exists() else set()
    by_source: dict[str, int] = {}
    for law in laws:
        by_source[law["source"]] = by_source.get(law["source"], 0) + 1
    return {
        "snapshot_date": repository_meta(path).get("snapshot_date", ""),
        "laws": len(laws),
        "listed": sum(1 for law in laws if law["is_listed"]),
        "with_content": sum(1 for law in laws if law["id"] in cached),
        "unreadable": len(cache.unreadable_ids()) if cache.path.exists() else 0,
        "by_source": dict(sorted(by_source.items())),
        "recent_changes": repository.read_all_changes(path)[-50:],
    }


@contextmanager
def single_instance(lock_name: str = LOCK_NATIONAL):
    """Allow one sync process per source at a time: two processes would double the request rate and could trip
    the source's rate limiting."""
    repository.LAWS_DIR.mkdir(parents=True, exist_ok=True)
    with open(repository.LAWS_DIR / lock_name, "w", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise AlreadyRunning("another sync process of the same kind is already running") from error
        yield


@contextmanager
def metadata_write_lock():
    """Held while the catalogue database is rewritten (read -> merge -> write back), so that syncs of two sources
    cannot overwrite each other's fresh writes."""
    repository.LAWS_DIR.mkdir(parents=True, exist_ok=True)
    with open(repository.LAWS_DIR / LOCK_METADATA_WRITE, "w", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        yield


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect Chinese laws, regulations and ministry rules")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("metadata", help="sync the whole national catalogue and record changes")
    content = commands.add_parser("content", help="fetch law texts in bulk")
    content.add_argument("--limit", type=int, default=500, help="maximum number of texts to fetch in this run")
    content.add_argument("--budget-seconds", type=float, default=1800, help="time limit of this run, in seconds")
    content.add_argument("--category", default=None, help="only fetch one category (a top_category value, e.g. 法律 or 行政法规)")
    content.add_argument(
        "--retry-unreadable", action="store_true",
        help="first clear the 'not a docx' unreadable records and fetch those laws again (needs LibreOffice for legacy docs)",
    )
    commands.add_parser("ministries", help="sync the ministry rule libraries")
    updater = commands.add_parser("update", help="scheduled run: catalogue diff + missing texts + ministry rules")
    updater.add_argument(
        "--content-limit", type=int, default=DEFAULT_UPDATE_CONTENT_LIMIT,
        help="maximum number of texts to fetch in this run; 0 fetches none",
    )
    updater.add_argument(
        "--budget-seconds", type=float, default=DEFAULT_UPDATE_BUDGET_SECONDS, help="time limit for fetching texts, in seconds"
    )
    updater.add_argument(
        "--retry-unreadable", action="store_true",
        help="first clear the 'not a docx' unreadable records and fetch those laws again (needs LibreOffice for legacy docs)",
    )
    commands.add_parser("probe", help="check whether each source is reachable right now")
    commands.add_parser("manifest", help="print the data manifest (JSON)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%m-%d %H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        if args.command == "update":
            return update(args.content_limit, args.budget_seconds, args.retry_unreadable)
        if args.command == "probe":
            return probe()
        if args.command == "manifest":
            sys.stdout.write(json.dumps(build_manifest(), ensure_ascii=False, indent=2) + "\n")
            return 0
        if args.command == "ministries":
            with single_instance(LOCK_MINISTRIES):
                return sync_ministries()
        with single_instance(LOCK_NATIONAL):
            if args.command == "metadata":
                return sync_metadata()
            return sync_content(args.limit, args.budget_seconds, args.category, args.retry_unreadable)
    except AlreadyRunning as error:
        logger.error("Not running: %s", error)
        return 1
    except flk_client.LawSourceBlocked as error:
        logger.error("Sync aborted: %s", error)
        return EXIT_BLOCKED
    except flk_client.LawSourceUnavailable as error:
        logger.error("Sync aborted: %s", error)
        return 1


if __name__ == "__main__":
    sys.exit(main())
