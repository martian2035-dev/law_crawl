"""Ministry rules: collect the rules in force from the "rule library" section of each ministry's official site.

The national database does not carry ministry rules, and the search endpoint of the national rule library is not
meant to be called directly, so each ministry's own rule library gets its own adapter.

- Ministry of Ecology and Environment, Ministry of Transport, Ministry of Emergency Management: paginated static
  lists; each item gives the title, the detail page, an enactment note (which order promulgated the rule on which
  date, and the date it takes effect) and a text file;
- Ministry of Housing and Urban-Rural Development, State Administration for Market Regulation: the section page
  loads list fragments from a content endpoint; the fragments have the same structure as above (the market
  regulator splits its rules into three sections: made after its formation, made before it, and made jointly);
- National Energy Administration: no rule library, only an "energy laws and regulations" page whose "rules and
  normative documents" chapters are a set of links compiled in 2017, without publication dates or validity
  status. Items are roughly split into ministry rules and normative documents by title, and their status is
  marked as unverified.

Texts come from the docx when one is offered; otherwise (the emergency ministry offers .wps, the market regulator
.doc) from the detail page (see html_text.py).
Adding a ministry: add an entry to SOURCES when its site matches one of the kinds above; otherwise write a new
fetch function.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import html
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import httpx

from law_crawl import repository
from law_crawl.docx_text import REASON_PAGE_GONE, LawTextError
from law_crawl.flk_client import MAX_DOCX_BYTES

# Category, issuer and status values are data: they stay in Chinese
TOP_CATEGORY = "部门规章"
REQUEST_INTERVAL_SECONDS = 1.5
REQUEST_TIMEOUT_SECONDS = 30
MAX_PAGES = 60
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
}
# List items: <li> on some sites, table rows on others
ITEM_SPLIT_PATTERN = re.compile(r"<(?:li|tr)\b", re.IGNORECASE)
DETAIL_LINK_PATTERN = re.compile(r"""<a[^>]+href=["']([^"']+\.s?html?)["'][^>]*>(.*?)</a>""", re.DOTALL | re.IGNORECASE)
# Enactment note: the element right after the title, starting with "（" and ending with "）". Notes quote the
# names of other rules, and some pages write them as <name> with unescaped angle brackets, so the note cannot be
# cut at the first "<"
NOTE_PATTERN = re.compile(r">\s*([（(].*?[）)])\s*</(?:p|i|span|div)>", re.DOTALL)
# Words a note must contain, to rule out other text that merely happens to be in parentheses
NOTE_KEYWORD_PATTERN = re.compile(r"\d{4}年|公布|发布")
COMMENT_PATTERN = re.compile(r"<!--.*?-->", re.DOTALL)
DOCX_LINK_PATTERN = re.compile(r"""href=["']([^"']+\.docx)["']""", re.IGNORECASE)
DATE_PATTERN = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日")
EFFECTIVE_PATTERN = re.compile(r"自(\d{4})年(\d{1,2})月(\d{1,2})日起(?:施行|实施|执行)")


@dataclass(frozen=True)
class RuleSource:
    """The rule library of one ministry."""

    key: str  # written to laws.source
    ministry: str  # written to laws.issuer
    list_url: str  # first list page (endpoint-based sources: the library page, used as Referer and base for relative links)
    min_expected: int  # fewer rules than this means the page layout changed: no update
    fetch: Callable[[httpx.Client, RuleSource, dt.date], list[dict]]
    next_page_url: str = ""  # static list pages from the second on; {index} starts at 1
    columns: tuple[UnitColumn, ...] = ()  # sections of an endpoint-based source
    content_hosts: tuple[str, ...] = ()  # hosts, besides that of list_url, texts may be fetched from


@dataclass(frozen=True)
class UnitColumn:
    """One section served by the content endpoint (page/build/unit of the jpaas publishing system); the parameters
    are the queryData written in the section page."""

    page_url: str  # section page address, used as Referer and base for relative links
    web_id: str
    tpl_set_id: str
    tag_id: str
    page_id: str


class MinistrySourceUnavailable(Exception):
    """The ministry site cannot serve data right now (network failure or changed page layout)."""


def rule_id(detail_url: str) -> str:
    """Rule id: MD5 of the detail page address (32 hex digits, the same format as ids in the national database)."""
    return hashlib.md5(detail_url.encode("utf-8"), usedforsecurity=False).hexdigest()


def to_iso(match: re.Match | None) -> str:
    if not match:
        return ""
    year, month, day = (int(part) for part in match.groups())
    try:
        return dt.date(year, month, day).isoformat()
    except ValueError:
        return ""


def parse_note(note: str) -> tuple[str, str]:
    """(publication date, effective date) from an enactment note.

    The publication date is the first date outside the "自……起施行" (effective from) clause, i.e. the original
    publication; an empty string when the note gives only an order number."""
    effective = EFFECTIVE_PATTERN.search(note)
    return to_iso(DATE_PATTERN.search(EFFECTIVE_PATTERN.sub("", note))), to_iso(effective)


def clean_text(fragment: str) -> str:
    return re.sub(r"\s+", "", html.unescape(re.sub(r"<[^>]+>", "", fragment)))


def parse_rule_list(page: str, page_url: str) -> list[dict]:
    """Rules in a list page: title, detail page address, enactment note, docx address (empty when absent).

    Only items with both a detail link and a note like "（……公布……）" count, so navigation and other list items
    are not mistaken for rules; template items that are commented out are removed first.
    """
    rules = []
    for chunk in ITEM_SPLIT_PATTERN.split(COMMENT_PATTERN.sub("", page))[1:]:
        link = DETAIL_LINK_PATTERN.search(chunk)
        note = NOTE_PATTERN.search(chunk)
        if not link or not note or not NOTE_KEYWORD_PATTERN.search(note.group(1)):
            continue
        docx = DOCX_LINK_PATTERN.search(chunk)
        rules.append(
            {
                "title": clean_text(link.group(2)),
                "url": urljoin(page_url, html.unescape(link.group(1))),
                "note": clean_text(note.group(1)),
                "docx_url": urljoin(page_url, html.unescape(docx.group(1))) if docx else "",
            }
        )
    return rules


def to_law(rule: dict, source: RuleSource, today: dt.date) -> dict:
    """Rule -> catalogue row (plus docx_url and text_url for fetching the text; those two are not stored)."""
    publish_date, effective_date = parse_note(rule["note"])
    pending = bool(effective_date) and effective_date > today.isoformat()
    return {
        "id": rule_id(rule["url"]),
        "title": rule["title"],
        "title_key": repository.title_key(rule["title"]),
        "top_category": TOP_CATEGORY,
        "category": f"{source.ministry}规章",
        "issuer": source.ministry,
        "region": "",
        "publish_date": publish_date,
        "effective_date": effective_date,
        # Rule libraries list only rules in force; one whose effective date is still ahead is marked not yet in force
        "status": repository.STATUS_PENDING if pending else repository.STATUS_EFFECTIVE,
        "source": source.key,
        "source_url": rule["url"],
        "docx_url": rule["docx_url"],
        "text_url": rule["url"],
    }


def new_client() -> httpx.Client:
    return httpx.Client(headers=HEADERS, timeout=REQUEST_TIMEOUT_SECONDS, follow_redirects=True)


def fetch_page(client: httpx.Client, url: str) -> str | None:
    """Fetch one list page; None on 404 (past the last page)."""
    try:
        response = client.get(url)
    except httpx.HTTPError as error:
        raise MinistrySourceUnavailable(f"cannot connect to {urlparse(url).hostname}: {type(error).__name__}") from error
    if response.status_code == 404:
        return None
    if response.status_code != 200:
        raise MinistrySourceUnavailable(f"{urlparse(url).hostname} returned an error (HTTP {response.status_code})")
    try:
        return response.content.decode("utf-8")
    except UnicodeDecodeError:
        return response.content.decode("gb18030", errors="replace")


def fetch_rules(client: httpx.Client, source: RuleSource, today: dt.date | None = None) -> list[dict]:
    """Fetch every rule of one ministry's rule library."""
    return source.fetch(client, source, today or dt.date.today())


def fetch_static_rules(client: httpx.Client, source: RuleSource, today: dt.date) -> list[dict]:
    """Static list: fetched page by page, de-duplicated by detail page address."""
    laws: dict[str, dict] = {}
    for index in range(MAX_PAGES):
        url = source.list_url if index == 0 else source.next_page_url.format(index=index)
        page = fetch_page(client, url)
        rules = parse_rule_list(page, url) if page else []
        if not rules:
            break
        for rule in rules:
            law = to_law(rule, source, today)
            laws.setdefault(law["id"], law)
        time.sleep(REQUEST_INTERVAL_SECONDS)
    return list(laws.values())


def is_source_host(url: str, source: RuleSource) -> bool:
    hosts = {urlparse(source.list_url).hostname, *source.content_hosts}
    return url.startswith("https://") and urlparse(url).hostname in hosts


def fetch_rule_page(client: httpx.Client, url: str, source: RuleSource) -> str:
    """Fetch a rule's detail page; only pages on the ministry's own site (the library host and content_hosts)."""
    if not is_source_host(url, source):
        raise MinistrySourceUnavailable("the rule page address is not on the ministry's own site")
    page = fetch_page(client, url)
    if page is None:
        # The page was taken down: handle it as unreadable, so it is recorded and not requested again
        raise LawTextError(REASON_PAGE_GONE)
    return page


def fetch_rule_docx(client: httpx.Client, docx_url: str, source: RuleSource) -> bytes:
    """Download a rule's docx text; only from the ministry's own site."""
    if not is_source_host(docx_url, source):
        raise MinistrySourceUnavailable("the rule file address is not on the ministry's own site")
    try:
        response = client.get(docx_url)
    except httpx.HTTPError as error:
        raise MinistrySourceUnavailable(f"rule file download failed: {type(error).__name__}") from error
    if response.status_code != 200:
        raise MinistrySourceUnavailable(f"rule file download failed (HTTP {response.status_code})")
    if len(response.content) > MAX_DOCX_BYTES:
        raise MinistrySourceUnavailable("the rule file exceeds the size limit")
    return response.content


# --- Endpoint-based sources (housing ministry, market regulator) ---

UNIT_API_PATH = "/api-gateway/jpaas-publish-server/front/page/build/unit"
UNIT_PAGE_SIZE = 50


def fetch_json(client: httpx.Client, method: str, url: str, **kwargs) -> dict:
    host = urlparse(url).hostname
    try:
        response = client.request(method, url, **kwargs)
    except httpx.HTTPError as error:
        raise MinistrySourceUnavailable(f"cannot connect to {host}: {type(error).__name__}") from error
    if response.status_code != 200:
        raise MinistrySourceUnavailable(f"{host} returned an error (HTTP {response.status_code})")
    try:
        return response.json()
    except ValueError as error:
        raise MinistrySourceUnavailable(f"{host} returned a response that cannot be parsed") from error


def fetch_unit_column(client: httpx.Client, column: UnitColumn, page_number: int) -> str:
    """Fetch the list fragment (HTML) of one page of a section."""
    params = {
        "parseType": "bulidstatic",
        "webId": column.web_id,
        "tplSetId": column.tpl_set_id,
        "pageType": "column",
        "tagId": column.tag_id,
        "editType": "null",
        "pageId": column.page_id,
        "paramJson": json.dumps({"pageNo": page_number, "pageSize": UNIT_PAGE_SIZE}),
    }
    payload = fetch_json(client, "GET", urljoin(column.page_url, UNIT_API_PATH), params=params,
                         headers={"Referer": column.page_url})
    data = payload.get("data")
    return data.get("html", "") if isinstance(data, dict) else ""


def fetch_unit_rules(client: httpx.Client, source: RuleSource, today: dt.date) -> list[dict]:
    """Endpoint-based source: list fragments fetched section by section, page by page (same structure as static lists)."""
    laws: dict[str, dict] = {}
    for column in source.columns:
        for page_number in range(1, MAX_PAGES + 1):
            rules = parse_rule_list(fetch_unit_column(client, column, page_number), column.page_url)
            for rule in rules:
                law = to_law(rule, source, today)
                laws.setdefault(law["id"], law)
            time.sleep(REQUEST_INTERVAL_SECONDS)
            if len(rules) < UNIT_PAGE_SIZE:
                break
    return list(laws.values())


# --- National Energy Administration: "rules and normative documents" on its energy law page ---

NORMATIVE_CATEGORY = "规范性文件"
STATUS_UNVERIFIED = "时效待核实"
NEA_SECTION_PATTERN = re.compile(
    r'''<div class="title">(.*?)</div>(.*?)(?=<div id="tab_\d+_\d+"|<div class="title">|\Z)''', re.DOTALL
)
NEA_ITEM_PATTERN = re.compile(r'''<li>\s*<a href="([^"]+)"[^>]*>(.*?)</a>''', re.DOTALL)
NEA_RULE_SECTION_KEYWORD = "规章"
# Titles with these words are notices, opinions and other normative documents; the rest count as ministry rules
NORMATIVE_TITLE_PATTERN = re.compile(r"关于|通知|意见|公告|批复|函$")


def fetch_nea_rules(client: httpx.Client, source: RuleSource, today: dt.date) -> list[dict]:
    """National Energy Administration page: the links under each part's "rules and normative documents" chapter.
    The page gives neither publication date nor validity status: the dates stay empty, the status is unverified."""
    page = fetch_page(client, source.list_url) or ""
    laws: dict[str, dict] = {}
    for section_title, body in NEA_SECTION_PATTERN.findall(COMMENT_PATTERN.sub("", page)):
        if NEA_RULE_SECTION_KEYWORD not in clean_text(section_title):
            continue
        for href, raw_title in NEA_ITEM_PATTERN.findall(body):
            title = clean_text(raw_title)
            url = re.sub(r"^http://", "https://", urljoin(source.list_url, html.unescape(href)))
            if not title:
                continue
            is_normative = bool(NORMATIVE_TITLE_PATTERN.search(title))
            laws.setdefault(rule_id(url), {
                "id": rule_id(url),
                "title": title,
                "title_key": repository.title_key(title),
                "top_category": NORMATIVE_CATEGORY if is_normative else TOP_CATEGORY,
                "category": f"{source.ministry}{NORMATIVE_CATEGORY if is_normative else '规章'}",
                "issuer": source.ministry,
                "region": "",
                "publish_date": "",
                "effective_date": "",
                "status": STATUS_UNVERIFIED,
                "source": source.key,
                "source_url": url,
                "docx_url": "",
                "text_url": url,
            })
    return list(laws.values())


# The three sections of the market regulator's rule library: rules made after the administration was formed,
# rules made before it, and rules made jointly with other bodies
SAMR_COLUMN_IDS = (
    ("https://www.samr.gov.cn/zw/zfxxgk/zc/gz/zjh/index.html", "2d373319375346bcac7595202c75f9f8"),
    ("https://www.samr.gov.cn/zw/zfxxgk/zc/gz/zjq/index.html", "8e50033366ea45da85de522ae76ac53b"),
    ("https://www.samr.gov.cn/zw/zfxxgk/zc/gz/lhzd/index.html", "e43c3cf1470a4fa194ebaa6404382c5d"),
)

SOURCES = (
    RuleSource("mee", "生态环境部", "https://www.mee.gov.cn/gzk/", 40, fetch_static_rules,
               "https://www.mee.gov.cn/gzk/index_{index}.shtml"),
    RuleSource("mot", "交通运输部", "https://xxgk.mot.gov.cn/2020/gz/", 150, fetch_static_rules,
               "https://xxgk.mot.gov.cn/2020/gz/index_{index}.html"),
    RuleSource("mem", "应急管理部", "https://www.mem.gov.cn/gk/zfxxgkpt/fdzdgknr/gz11/index.shtml", 40, fetch_static_rules,
               "https://www.mem.gov.cn/gk/zfxxgkpt/fdzdgknr/gz11/index_{index}.shtml"),
    RuleSource(
        "mohurd", "住房和城乡建设部", "https://www.mohurd.gov.cn/gongkai/zhengce/gzk/index.html", 50, fetch_unit_rules,
        columns=(UnitColumn("https://www.mohurd.gov.cn/gongkai/zhengce/gzk/index.html", "86ca573ec4df405db627fdc2493677f3",
                            "fc259c381af3496d85e61997ea7771cb", "内容1", "DDOs7kRcD3aqX6vDea1GY"),),
    ),
    RuleSource(
        "samr", "市场监管总局", "https://www.samr.gov.cn/zw/zfxxgk/zc/gz/index.html", 150, fetch_unit_rules,
        columns=tuple(
            UnitColumn(page_url, "29e9522dc89d4e088a953d8cede72f4c", "5c30fb89ae5e48b9aefe3cdf49853830", "当前内容", page_id)
            for page_url, page_id in SAMR_COLUMN_IDS
        ),
    ),
    RuleSource("nea", "国家能源局", "https://www.nea.gov.cn/nyflfg/index.htm", 20, fetch_nea_rules,
               content_hosts=("zfxxgk.nea.gov.cn",)),
)
