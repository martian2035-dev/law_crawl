"""国家法律法规数据库（flk.npc.gov.cn）接口客户端。

只读取公开接口：分类与制定机关枚举、法规列表、法规详情、正文 docx 下载地址。
法规正文属于官方文件，各条记录保留官方详情页链接以便核对。
"""

from __future__ import annotations

from urllib.parse import quote, urlparse

import httpx

BASE_URL = "https://flk.npc.gov.cn"
ENUM_URL = f"{BASE_URL}/law-search/search/enumData"
LIST_URL = f"{BASE_URL}/law-search/search/list"
DETAIL_URL = f"{BASE_URL}/law-search/search/flfgDetails"
DOWNLOAD_URL = f"{BASE_URL}/law-search/download/pc"
SOURCE_NAME = "国家法律法规数据库（flk.npc.gov.cn）"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Referer": f"{BASE_URL}/",
}
REQUEST_TIMEOUT_SECONDS = 30
LIST_PAGE_SIZE = 500
MAX_DOCX_BYTES = 30 * 1024 * 1024
# 正文文件所在的对象存储域名后缀；下载地址不在其中时拒绝请求
ALLOWED_FILE_HOST_SUFFIXES = (".cucloud.cn", ".npc.gov.cn")
SUCCESS_CODE = 200


# 官方库在访问过于频繁时返回的浏览器验证页里的固定文字
CHALLENGE_MARKER = "Please enable JavaScript"


class LawSourceUnavailable(Exception):
    """官方数据库暂时无法提供数据（网络故障、接口变化或返回内容异常）。"""


class LawSourceBlocked(LawSourceUnavailable):
    """官方数据库要求浏览器验证：说明访问过于频繁，应立即停止并隔一段时间再试，不能绕过。"""


def official_url(law_id: str, title: str) -> str:
    """官方详情页链接。"""
    return f"{BASE_URL}/detail?id={law_id}&title={quote(title)}"


def new_client() -> httpx.Client:
    return httpx.Client(headers=HEADERS, timeout=REQUEST_TIMEOUT_SECONDS, follow_redirects=True)


def _json(response: httpx.Response) -> dict:
    if response.status_code != 200:
        raise LawSourceUnavailable(f"官方数据库返回错误（HTTP {response.status_code}）")
    try:
        return response.json()
    except ValueError as error:
        if CHALLENGE_MARKER in response.text:
            raise LawSourceBlocked("官方数据库要求浏览器验证（访问过于频繁），已停止取数，请隔一段时间再试") from error
        raise LawSourceUnavailable("官方数据库返回内容无法解析") from error


def _request(client: httpx.Client, method: str, url: str, **kwargs) -> dict:
    try:
        return _json(client.request(method, url, **kwargs))
    except httpx.HTTPError as error:
        raise LawSourceUnavailable(f"无法连接官方数据库：{type(error).__name__}") from error


def fetch_enums(client: httpx.Client) -> dict:
    """法规分类树（flfgfl）与制定机关树（zdjgfl）。"""
    payload = _request(client, "GET", ENUM_URL)
    if payload.get("code") != SUCCESS_CODE or "data" not in payload:
        raise LawSourceUnavailable("官方数据库的分类枚举接口返回异常")
    return payload["data"]


def fetch_list_page(client: httpx.Client, page: int, issuer_codes: list[int] | None = None) -> dict:
    """一页法规列表（按公布日期倒序）；issuer_codes 为制定机关代码筛选。返回 {total, rows}。"""
    body = {
        "searchRange": 1,
        "sxrq": [],
        "gbrq": [],
        "searchType": 2,
        "sxx": [],
        "gbrqYear": [],
        "flfgCodeId": [],
        "zdjgCodeId": issuer_codes or [],
        "searchContent": "",
        "orderByParam": {"order": "-1", "sort": ""},
        "pageNum": page,
        "pageSize": LIST_PAGE_SIZE,
    }
    payload = _request(client, "POST", LIST_URL, json=body)
    if "rows" not in payload or "total" not in payload:
        raise LawSourceUnavailable("官方数据库的列表接口返回异常")
    return payload


def fetch_detail(client: httpx.Client, law_id: str) -> dict:
    """法规详情（元数据、正文文件路径、历史沿革）。"""
    payload = _request(client, "GET", DETAIL_URL, params={"bbbs": law_id})
    if payload.get("code") != SUCCESS_CODE or not payload.get("data"):
        raise LawSourceUnavailable("官方数据库的详情接口返回异常")
    return payload["data"]


def fetch_docx(client: httpx.Client, law_id: str) -> bytes:
    """下载法规正文的 docx 文件内容。"""
    payload = _request(client, "GET", DOWNLOAD_URL, params={"format": "docx", "bbbs": law_id, "fileId": ""})
    file_url = (payload.get("data") or {}).get("url")
    if not file_url:
        raise LawSourceUnavailable("官方数据库没有返回正文文件地址")
    parsed = urlparse(file_url)
    if parsed.scheme != "https" or not (parsed.hostname or "").endswith(ALLOWED_FILE_HOST_SUFFIXES):
        raise LawSourceUnavailable("正文文件地址不在允许的域名范围内")
    try:
        response = client.get(file_url, headers={"Referer": f"{BASE_URL}/"})
    except httpx.HTTPError as error:
        raise LawSourceUnavailable(f"正文文件下载失败：{type(error).__name__}") from error
    if response.status_code != 200:
        raise LawSourceUnavailable(f"正文文件下载失败（HTTP {response.status_code}）")
    if len(response.content) > MAX_DOCX_BYTES:
        raise LawSourceUnavailable("正文文件超过大小上限")
    return response.content
