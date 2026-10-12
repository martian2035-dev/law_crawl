"""法律、法规、规章的采集。

    python -m law_crawl update            # 定时增量：目录比对 + 取缺的正文 + 部门规章
    python -m law_crawl metadata          # 只同步国家法律法规数据库的目录 → data/laws.db
    python -m law_crawl content --limit 500 --budget-seconds 1800   # 只取正文 → data/laws_content.db（可多次续跑）
    python -m law_crawl ministries        # 只同步各部门规章库
    python -m law_crawl probe             # 各数据源各发一个请求，看当前网络能否访问
    python -m law_crawl manifest          # 输出数据清单（条数、快照日期），供流水线写入 manifest.json

目录同步（metadata）：国家法律法规数据库（flk.npc.gov.cn）没有“最近变更”接口，旧法规的时效性会变而公布日期不变，
所以每次都全量拉目录（约 125 次请求）再与现有库比对：新增的、时效性变了的、不再收录的各记一条变更
（law_changes 表）；不再收录的法规保留在库里并标记 is_listed = 0，不删除。写入临时库并校验后替换正式库。

正文（content）：按“全国性法规优先、现行优先”的顺序取回尚未缓存的法规正文，到达条数或时间上限后停止，下次运行
接着取。连续失败时先暂停几分钟再继续，多次暂停仍不恢复才停止；遇到浏览器验证则立即停止。官方只提供旧版 doc 等
无法解析的文件会被记下来，之后不再重复下载。

部门规章（ministries）：国家法律法规数据库不收录部门规章，按部门从其官网的规章库采集（见 ministry_rules.py），
与上面的法规存在同一个库里，用 source 列区分来源。

访问限制：国家法律法规数据库在访问频繁时会返回浏览器验证页并拒绝服务。采集遇到验证页立即停止（退出码 2），
同一来源同一时刻只允许一个进程，请求间隔见下面的常量。不要调高频率，也不要绕过验证。
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
from law_crawl.docx_text import LawTextError, extract_paragraphs
from law_crawl.html_text import extract_article_paragraphs
from law_crawl.repository import ContentCache

REGION_DATA_PATH = Path(__file__).with_name("china_region_centers.json")

logger = logging.getLogger("laws_sync")

# 请求间隔：官方库在访问频繁时会弹出浏览器验证并拒绝服务，宁可慢
REQUEST_INTERVAL_SECONDS = 1.0
CONTENT_INTERVAL_SECONDS = 2.0
# 进程锁：国家法律法规数据库的取数与各部门规章库的取数各一把，互不妨碍；改写元数据库时另有一把短时间持有的锁
LOCK_NATIONAL = "laws_sync.lock"
LOCK_MINISTRIES = "laws_sync_ministries.lock"
LOCK_METADATA_WRITE = "laws_metadata_write.lock"
EXIT_BLOCKED = 2
MAX_CONSECUTIVE_FAILURES = 10
# 连续失败多半是网络临时中断：先停一会儿再继续，累计停够次数仍不恢复才放弃
FAILURE_PAUSE_SECONDS = 300
MAX_FAILURE_PAUSES = 6
PROGRESS_EVERY = 200
DEFAULT_UPDATE_CONTENT_LIMIT = 100000
DEFAULT_UPDATE_BUDGET_SECONDS = 6 * 3600
MIN_EXPECTED_LAWS = 20000
# 一次同步中“官方库不再收录”的法规超过这个比例时视为数据源异常，不替换现有库
MAX_REMOVED_RATIO = 0.05
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
# 批量预取正文时的类别先后
CATEGORY_PRIORITY = ("宪法", "法律", "行政法规", "监察法规", "司法解释", "部门规章", "规范性文件", "地方法规")


class AlreadyRunning(Exception):
    """另一个同步进程持有锁。"""


def category_names(category_tree: dict) -> dict[int, tuple[str, str]]:
    """分类代码 → （一级类别, 本级类别名）。"""
    names: dict[int, tuple[str, str]] = {}

    def walk(node: dict, top: str | None) -> None:
        for child in node.get("children") or []:
            top_name = top or child["name"]
            names[child["codeId"]] = (top_name, child["name"])
            walk(child, top_name)

    walk(category_tree, None)
    return names


def province_issuers(issuer_tree: dict) -> list[tuple[int, str]]:
    """省级地方制定机关：（代码, 地区名）。"""
    for group in issuer_tree.get("children") or []:
        if group["name"] == LOCAL_ISSUER_GROUP:
            return [(child["codeId"], child["name"]) for child in group.get("children") or []]
    return []


@lru_cache(maxsize=1)
def load_regions() -> dict:
    """行政区名称数据（省 → 市 → 区县），用来由制定机关名称判断地方法规所属地区。"""
    return json.loads(REGION_DATA_PATH.read_text(encoding="utf-8"))


def place_index(province_names: list[str]) -> list[tuple[str, str]]:
    """（地名, 地区名）对照，长地名在前：省、设区的市、自治州、自治县（旗）→ 官方库的地区名。

    官方库按省级制定机关筛选时不含部分设区的市，这些法规的地区由制定机关名称的开头确定。
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
    """翻页取回某个筛选条件下的全部列表行。"""
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
    """列表行 → 元数据库的一行。分类代码不在分类树中时，有地区的归入地方法规，否则归入其他。"""
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
    """取回全部法规元数据，并确定地方法规所属地区。"""
    enums = flk_client.fetch_enums(client)
    categories = category_names(enums["flfgfl"])
    rows = fetch_all_rows(client)
    logger.info("取回法规 %d 条，开始按地区归类", len(rows))
    regions: dict[str, str] = {}
    for code, name in province_issuers(enums["zdjgfl"]):
        province_rows = fetch_all_rows(client, [code])
        regions.update({row["bbbs"]: name for row in province_rows})
        logger.info("  %s %d 条", name, len(province_rows))
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
    """把某个来源本次取回的目录与现有库合并，返回（合并后的全部法规, 本次发现的变更）。

    只比对同一来源的法规，其他来源的原样保留。该来源在现有库中没有任何记录（首次收录）时不逐条记“新增”。
    来源不再收录的法规保留并标记 is_listed = 0。
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
    """现有元数据库的说明信息；库不存在时为空。"""
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
    """全量拉取目录并与现有库合并；数据源异常（条数过少、大批消失）时不替换现有库。"""
    with flk_client.new_client() as client:
        current = collect_laws(client)
    if len(current) < MIN_EXPECTED_LAWS:
        logger.error("只取回 %d 条，少于预期的 %d 条，不替换现有数据库", len(current), MIN_EXPECTED_LAWS)
        return 1
    with metadata_write_lock():
        target = repository.METADATA_DB_PATH
        previous = repository.read_all_laws(target)
        merged, changes = merge_with_previous(previous, current, (today or dt.date.today()).isoformat())
        counts = count_changes(changes)
        listed_before = sum(1 for law in previous if law["is_listed"] and law["source"] == repository.SOURCE_FLK)
        if listed_before and counts[repository.CHANGE_REMOVED] > listed_before * MAX_REMOVED_RATIO:
            logger.error(
                "官方库不再收录的法规有 %d 部，超过现有 %d 部的 %.0f%%，视为数据源异常，不替换现有数据库",
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
        "目录已更新：现收录 %d 部，新增 %d，时效性变化 %d，不再收录 %d，重新收录 %d",
        len(current), counts[repository.CHANGE_ADDED], counts[repository.CHANGE_STATUS],
        counts[repository.CHANGE_REMOVED], counts[repository.CHANGE_RESTORED],
    )
    for item in changes[:20]:
        logger.info("  [%s] %s %s → %s", item["change_type"], item["title"], item["old_value"] or "—", item["new_value"] or "—")
    return 0


def pending_content_ids(cache: ContentCache, category: str | None) -> list[str]:
    """尚未缓存正文的法规编号，全国性法规、现行法规在前。"""
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


def sync_content(limit: int, budget_seconds: float, category: str | None) -> int:
    cache = ContentCache()
    pending = pending_content_ids(cache, category)
    logger.info("待取正文 %d 部，本次最多 %d 部、%d 秒", len(pending), limit, budget_seconds)
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
                # 文件取到了但不是 docx：记下来以后跳过，不算数据源故障
                cache.mark_unreadable(law_id, str(error))
                unreadable += 1
                consecutive_failures = 0
            except flk_client.LawSourceBlocked:
                logger.info("本次取回 %d 部后遇到浏览器验证，停止", fetched)
                raise
            except flk_client.LawSourceUnavailable as error:
                failed += 1
                consecutive_failures += 1
                logger.warning("  %s 正文未取到：%s", law_id, error)
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    if pauses >= MAX_FAILURE_PAUSES:
                        logger.error("连续 %d 部失败，暂停 %d 次后仍未恢复，停止", consecutive_failures, pauses)
                        stopped_by_failures = True
                        break
                    pauses += 1
                    consecutive_failures = 0
                    logger.warning("连续 %d 部失败，暂停 %d 秒后继续（第 %d 次）", MAX_CONSECUTIVE_FAILURES, FAILURE_PAUSE_SECONDS, pauses)
                    time.sleep(FAILURE_PAUSE_SECONDS)
            if (fetched + unreadable) % PROGRESS_EVERY == 0 and fetched + unreadable:
                logger.info("  已处理 %d / %d", fetched + unreadable + failed, min(limit, len(pending)))
            time.sleep(CONTENT_INTERVAL_SECONDS)
    logger.info(
        "本次取回 %d 部，无法解析 %d 部，失败 %d 部，剩余 %d 部",
        fetched, unreadable, failed, len(pending) - fetched - unreadable,
    )
    return 1 if stopped_by_failures else 0


def write_ministry_rules(source: ministry_rules.RuleSource, rules: list[dict], today: dt.date) -> dict[str, int]:
    """把一个部门的规章并入元数据库，返回各类变更的条数。"""
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
    """规章正文段落：有 docx 文字版时取 docx，否则从详情网页取。"""
    if rule["docx_url"]:
        return extract_paragraphs(ministry_rules.fetch_rule_docx(client, rule["docx_url"], source))
    return extract_article_paragraphs(ministry_rules.fetch_rule_page(client, rule["text_url"], source))


def fetch_ministry_contents(client: httpx.Client, source: ministry_rules.RuleSource, rules: list[dict], cache: ContentCache) -> int:
    """取回还没有缓存正文的规章，返回取回的部数。正文不在该部门官网上的（专栏里链接到其他网站的）不取。"""
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
            logger.warning("  %s 正文未取到：%s", rule["title"], error)
        time.sleep(ministry_rules.REQUEST_INTERVAL_SECONDS)
    return fetched


def sync_ministries(today: dt.date | None = None) -> int:
    """同步各部门规章库的规章目录与正文；某个部门失败不影响其他部门。"""
    today = today or dt.date.today()
    if not repository.METADATA_DB_PATH.exists():
        logger.error("法规元数据库不存在，请先运行 metadata")
        return 1
    status = 0
    cache = ContentCache()
    with ministry_rules.new_client() as client:
        for source in ministry_rules.SOURCES:
            try:
                rules = ministry_rules.fetch_rules(client, source, today)
            except ministry_rules.MinistrySourceUnavailable as error:
                logger.error("%s规章库同步失败：%s", source.ministry, error)
                status = 1
                continue
            if len(rules) < source.min_expected:
                logger.error("%s规章库只取到 %d 部，少于预期的 %d 部，不更新", source.ministry, len(rules), source.min_expected)
                status = 1
                continue
            counts = write_ministry_rules(source, rules, today)
            fetched = fetch_ministry_contents(client, source, rules, cache)
            logger.info(
                "%s规章库：%d 部，新增 %d，时效性变化 %d，不再收录 %d；本次取回正文 %d 部",
                source.ministry, len(rules), counts[repository.CHANGE_ADDED], counts[repository.CHANGE_STATUS],
                counts[repository.CHANGE_REMOVED], fetched,
            )
    return status


def sync_ministries_exclusively() -> int:
    """同步部门规章；已有进程在同步部门规章时本次跳过。"""
    try:
        with single_instance(LOCK_MINISTRIES):
            return sync_ministries()
    except AlreadyRunning:
        logger.info("已有进程在同步部门规章，本次跳过这一步")
        return 0


def sync_national(content_limit: int, budget_seconds: float) -> int:
    """国家法律法规数据库：目录比对，成功后再取缺的正文。遇到浏览器验证返回 EXIT_BLOCKED。

    已有进程在取国家法律法规数据库（例如正文全量下载还没跑完）时本次跳过，不算失败。
    """
    try:
        with single_instance(LOCK_NATIONAL):
            if sync_metadata() != 0:
                return 1
            return sync_content(content_limit, budget_seconds, None) if content_limit > 0 else 0
    except AlreadyRunning:
        logger.info("已有进程在同步国家法律法规数据库，本次跳过这一步")
        return 0
    except flk_client.LawSourceBlocked as error:
        logger.error("国家法律法规数据库同步中止：%s", error)
        return EXIT_BLOCKED
    except flk_client.LawSourceUnavailable as error:
        logger.error("国家法律法规数据库同步中止：%s", error)
        return 1


def update(content_limit: int, budget_seconds: float) -> int:
    """定时增量：国家法律法规数据库 → 各部门规章库。

    两步互不拖累：前一步失败后一步照常执行，最后返回第一个非零的退出码。
    """
    statuses = [sync_national(content_limit, budget_seconds), sync_ministries_exclusively()]
    return next((status for status in statuses if status), 0)


PROBE_TIMEOUT_SECONDS = 20


def probe() -> int:
    """各数据源各发一个请求，报告当前网络能否访问；全部可达返回 0。"""
    reachable = True
    with flk_client.new_client() as client:
        try:
            flk_client.fetch_enums(client)
            logger.info("可访问   国家法律法规数据库")
        except flk_client.LawSourceUnavailable as error:
            reachable = False
            logger.info("不可访问 国家法律法规数据库：%s", error)
    with ministry_rules.new_client() as client:
        for source in ministry_rules.SOURCES:
            try:
                response = client.get(source.list_url, timeout=PROBE_TIMEOUT_SECONDS)
                ok = response.status_code == 200
                detail = f"HTTP {response.status_code}"
            except httpx.HTTPError as error:
                ok, detail = False, type(error).__name__
            reachable = reachable and ok
            logger.info("%s %s规章库：%s", "可访问  " if ok else "不可访问", source.ministry, detail)
    return 0 if reachable else 1


def build_manifest() -> dict:
    """数据清单：目录快照日期、各来源条数、已有正文的部数、最近的变更。"""
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
    """同一来源同一时刻只允许一个同步进程：两个进程同时取数会让请求频率翻倍，容易触发对方的访问限制。"""
    repository.LAWS_DIR.mkdir(parents=True, exist_ok=True)
    with open(repository.LAWS_DIR / lock_name, "w", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise AlreadyRunning("已有一个同类的法规同步进程在运行") from error
        yield


@contextmanager
def metadata_write_lock():
    """改写元数据库（读出 → 合并 → 写回）期间持有，防止两个来源的同步互相覆盖对方刚写入的内容。"""
    repository.LAWS_DIR.mkdir(parents=True, exist_ok=True)
    with open(repository.LAWS_DIR / LOCK_METADATA_WRITE, "w", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        yield


def main() -> int:
    parser = argparse.ArgumentParser(description="法律法规数据同步")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("metadata", help="全量同步法规目录并记录变更")
    content = commands.add_parser("content", help="批量取回法规正文")
    content.add_argument("--limit", type=int, default=500, help="本次最多取多少部")
    content.add_argument("--budget-seconds", type=float, default=1800, help="本次最长运行秒数")
    content.add_argument("--category", default=None, help="只取某一类别，如 法律、行政法规")
    commands.add_parser("ministries", help="同步各部门规章库的规章")
    updater = commands.add_parser("update", help="定时增量：目录比对 + 取缺的正文 + 部门规章")
    updater.add_argument("--content-limit", type=int, default=DEFAULT_UPDATE_CONTENT_LIMIT, help="本次最多取多少部正文，0 表示不取")
    updater.add_argument("--budget-seconds", type=float, default=DEFAULT_UPDATE_BUDGET_SECONDS, help="取正文的最长秒数")
    commands.add_parser("probe", help="检查各数据源当前能否访问")
    commands.add_parser("manifest", help="输出数据清单（JSON）")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%m-%d %H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        if args.command == "update":
            return update(args.content_limit, args.budget_seconds)
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
            return sync_content(args.limit, args.budget_seconds, args.category)
    except AlreadyRunning as error:
        logger.error("%s，本次不执行", error)
        return 1
    except flk_client.LawSourceBlocked as error:
        logger.error("同步中止：%s", error)
        return EXIT_BLOCKED
    except flk_client.LawSourceUnavailable as error:
        logger.error("同步中止：%s", error)
        return 1


if __name__ == "__main__":
    sys.exit(main())
