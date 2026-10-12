"""部门规章：从各部门官网的“规章库”栏目采集现行有效规章。

国家法律法规数据库不收录部门规章，国家规章库的检索接口又不宜直接调用，所以按部门逐个适配其官网的规章库。

- 生态环境部、交通运输部、应急管理部：分页的静态列表，每条给出标题、详情页、括注
  （“某年某月某日某部令第 N 号公布（发布），……自某年某月某日起施行”）和文字版文件；
- 住房和城乡建设部、市场监管总局：栏目页面由脚本调用内容接口取回列表片段，片段结构与上面相同
  （市场监管总局的规章分“总局组建后制定、组建前制定、联合制定”三个栏目）；
- 国家能源局：没有规章库，只有“能源法律法规专栏”，其中“规章及规范性文件”一章是 2017 年整理的
  一批链接，没有公布日期和时效性，按标题粗分为部门规章和规范性文件，时效性标为待核实。

正文优先取 docx 文字版；没有 docx 的（应急管理部是 wps、市场监管总局是 doc）改从详情网页取（见 html_text.py）。
接入新部门：结构与上述某一类相同时在 SOURCES 里加一项；不同的另写取数函数。
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
from law_crawl.docx_text import LawTextError
from law_crawl.flk_client import MAX_DOCX_BYTES

TOP_CATEGORY = "部门规章"
REQUEST_INTERVAL_SECONDS = 1.5
REQUEST_TIMEOUT_SECONDS = 30
MAX_PAGES = 60
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
}
# 列表项：有的站点用 <li>，有的用表格行
ITEM_SPLIT_PATTERN = re.compile(r"<(?:li|tr)\b", re.IGNORECASE)
DETAIL_LINK_PATTERN = re.compile(r"""<a[^>]+href=["']([^"']+\.s?html?)["'][^>]*>(.*?)</a>""", re.DOTALL | re.IGNORECASE)
# 括注：紧跟在标题后的一个元素，整段以“（”开头、以“）”结尾。括注里会引用其他规章的名称，
# 有的页面直接写成 <某某规定>（未转义的尖括号），所以不能按“不含 <”来截取
NOTE_PATTERN = re.compile(r">\s*([（(].*?[）)])\s*</(?:p|i|span|div)>", re.DOTALL)
# 括注里应当出现的字样，用来排除恰好带括号的其他文字
NOTE_KEYWORD_PATTERN = re.compile(r"\d{4}年|公布|发布")
COMMENT_PATTERN = re.compile(r"<!--.*?-->", re.DOTALL)
DOCX_LINK_PATTERN = re.compile(r"""href=["']([^"']+\.docx)["']""", re.IGNORECASE)
DATE_PATTERN = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日")
EFFECTIVE_PATTERN = re.compile(r"自(\d{4})年(\d{1,2})月(\d{1,2})日起(?:施行|实施|执行)")


@dataclass(frozen=True)
class RuleSource:
    """一个部门的规章库。"""

    key: str  # 写入 laws.source
    ministry: str
    list_url: str  # 列表第 1 页（接口类来源为规章库页面地址，用作 Referer 和相对地址的基准）
    min_expected: int  # 少于这个数视为页面结构变了，不更新
    fetch: Callable[[httpx.Client, RuleSource, dt.date], list[dict]]
    next_page_url: str = ""  # 静态列表第 2 页起的地址，{index} 从 1 开始
    columns: tuple[UnitColumn, ...] = ()  # 内容接口类来源的栏目
    content_hosts: tuple[str, ...] = ()  # 除 list_url 所在主机外，还允许从这些主机取正文


@dataclass(frozen=True)
class UnitColumn:
    """内容接口（jpaas 发布系统的 page/build/unit）的一个栏目；参数取自栏目页面里写明的 queryData。"""

    page_url: str  # 栏目页面地址，用作 Referer 和相对链接的基准
    web_id: str
    tpl_set_id: str
    tag_id: str
    page_id: str


class MinistrySourceUnavailable(Exception):
    """部门官网暂时无法提供数据（网络故障、页面结构变化）。"""


def rule_id(detail_url: str) -> str:
    """规章编号：详情页地址的 MD5（32 位十六进制，与国家法律法规数据库的编号格式一致）。"""
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
    """由括注取（公布日期, 施行日期）。

    公布日期取“自……起施行”以外的第一个日期（最初公布的日期）；括注只写了令号、没写公布日期时为空串。"""
    effective = EFFECTIVE_PATTERN.search(note)
    return to_iso(DATE_PATTERN.search(EFFECTIVE_PATTERN.sub("", note))), to_iso(effective)


def clean_text(fragment: str) -> str:
    return re.sub(r"\s+", "", html.unescape(re.sub(r"<[^>]+>", "", fragment)))


def parse_rule_list(page: str, page_url: str) -> list[dict]:
    """列表页中的规章：标题、详情页地址、括注、docx 地址（没有时为空串）。

    只认同时带详情链接和“（……公布……）”这类括注的列表项，导航等其他列表项不会被当成规章；
    注释掉的模板条目先去除。
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
    """规章 → 法规元数据库的一行（另带 docx_url、text_url 供取正文用，不入库）。"""
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
        # 规章库只列现行有效的规章；施行日期未到的标为尚未生效
        "status": repository.STATUS_PENDING if pending else repository.STATUS_EFFECTIVE,
        "source": source.key,
        "source_url": rule["url"],
        "docx_url": rule["docx_url"],
        "text_url": rule["url"],
    }


def new_client() -> httpx.Client:
    return httpx.Client(headers=HEADERS, timeout=REQUEST_TIMEOUT_SECONDS, follow_redirects=True)


def fetch_page(client: httpx.Client, url: str) -> str | None:
    """取一页列表；404 返回 None（翻到头了）。"""
    try:
        response = client.get(url)
    except httpx.HTTPError as error:
        raise MinistrySourceUnavailable(f"无法连接 {urlparse(url).hostname}：{type(error).__name__}") from error
    if response.status_code == 404:
        return None
    if response.status_code != 200:
        raise MinistrySourceUnavailable(f"{urlparse(url).hostname} 返回错误（HTTP {response.status_code}）")
    try:
        return response.content.decode("utf-8")
    except UnicodeDecodeError:
        return response.content.decode("gb18030", errors="replace")


def fetch_rules(client: httpx.Client, source: RuleSource, today: dt.date | None = None) -> list[dict]:
    """取回一个部门规章库的全部规章。"""
    return source.fetch(client, source, today or dt.date.today())


def fetch_static_rules(client: httpx.Client, source: RuleSource, today: dt.date) -> list[dict]:
    """静态列表：逐页取回（按详情页地址去重）。"""
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
    """取规章详情网页；只取该部门官网（规章库所在主机及 content_hosts）上的页面。"""
    if not is_source_host(url, source):
        raise MinistrySourceUnavailable("规章页面地址不在该部门官网上")
    page = fetch_page(client, url)
    if page is None:
        # 页面已被撤下：按“无法解析”处理，记下来以后不再重复请求
        raise LawTextError("规章页面不存在（HTTP 404）")
    return page


def fetch_rule_docx(client: httpx.Client, docx_url: str, source: RuleSource) -> bytes:
    """下载规章的 docx 文字版；只从该部门官网下载。"""
    if not is_source_host(docx_url, source):
        raise MinistrySourceUnavailable("规章文件地址不在该部门官网上")
    try:
        response = client.get(docx_url)
    except httpx.HTTPError as error:
        raise MinistrySourceUnavailable(f"规章文件下载失败：{type(error).__name__}") from error
    if response.status_code != 200:
        raise MinistrySourceUnavailable(f"规章文件下载失败（HTTP {response.status_code}）")
    if len(response.content) > MAX_DOCX_BYTES:
        raise MinistrySourceUnavailable("规章文件超过大小上限")
    return response.content


# --- 内容接口类来源（住房和城乡建设部、市场监管总局） ---

UNIT_API_PATH = "/api-gateway/jpaas-publish-server/front/page/build/unit"
UNIT_PAGE_SIZE = 50


def fetch_json(client: httpx.Client, method: str, url: str, **kwargs) -> dict:
    host = urlparse(url).hostname
    try:
        response = client.request(method, url, **kwargs)
    except httpx.HTTPError as error:
        raise MinistrySourceUnavailable(f"无法连接 {host}：{type(error).__name__}") from error
    if response.status_code != 200:
        raise MinistrySourceUnavailable(f"{host} 返回错误（HTTP {response.status_code}）")
    try:
        return response.json()
    except ValueError as error:
        raise MinistrySourceUnavailable(f"{host} 返回内容无法解析") from error


def fetch_unit_column(client: httpx.Client, column: UnitColumn, page_number: int) -> str:
    """取栏目某一页的列表片段（HTML）。"""
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
    """内容接口类来源：逐栏目、逐页取回列表片段（结构同静态列表）。"""
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


# --- 国家能源局：能源法律法规专栏的“规章及规范性文件” ---

NORMATIVE_CATEGORY = "规范性文件"
STATUS_UNVERIFIED = "时效待核实"
NEA_SECTION_PATTERN = re.compile(
    r'''<div class="title">(.*?)</div>(.*?)(?=<div id="tab_\d+_\d+"|<div class="title">|\Z)''', re.DOTALL
)
NEA_ITEM_PATTERN = re.compile(r'''<li>\s*<a href="([^"]+)"[^>]*>(.*?)</a>''', re.DOTALL)
NEA_RULE_SECTION_KEYWORD = "规章"
# 标题带这些字样的是通知、意见一类的规范性文件，其余按部门规章归类
NORMATIVE_TITLE_PATTERN = re.compile(r"关于|通知|意见|公告|批复|函$")


def fetch_nea_rules(client: httpx.Client, source: RuleSource, today: dt.date) -> list[dict]:
    """国家能源局专栏：各篇“规章及规范性文件”一章下的链接。专栏不给公布日期和时效性，两项留空 / 标为待核实。"""
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


SAMR_COLUMN_IDS = (
    ("https://www.samr.gov.cn/zw/zfxxgk/zc/gz/zjh/index.html", "2d373319375346bcac7595202c75f9f8"),  # 总局组建后制定
    ("https://www.samr.gov.cn/zw/zfxxgk/zc/gz/zjq/index.html", "8e50033366ea45da85de522ae76ac53b"),  # 总局组建前制定
    ("https://www.samr.gov.cn/zw/zfxxgk/zc/gz/lhzd/index.html", "e43c3cf1470a4fa194ebaa6404382c5d"),  # 联合制定
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
