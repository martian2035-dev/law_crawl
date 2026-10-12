"""Client for the National Database of Laws and Regulations (flk.npc.gov.cn).

Only public endpoints are read: the category and issuer enumerations, the law list, law details and the download
address of the docx text. Law texts are official documents; every record keeps the link to its official detail
page so it can be checked against the source.
"""

from __future__ import annotations

from urllib.parse import quote, urlparse

import httpx

BASE_URL = "https://flk.npc.gov.cn"
ENUM_URL = f"{BASE_URL}/law-search/search/enumData"
LIST_URL = f"{BASE_URL}/law-search/search/list"
DETAIL_URL = f"{BASE_URL}/law-search/search/flfgDetails"
DOWNLOAD_URL = f"{BASE_URL}/law-search/download/pc"
# Written to the meta table of laws.db as the name of the source; data values stay in Chinese
SOURCE_NAME = "国家法律法规数据库（flk.npc.gov.cn）"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Referer": f"{BASE_URL}/",
}
REQUEST_TIMEOUT_SECONDS = 30
LIST_PAGE_SIZE = 500
MAX_DOCX_BYTES = 30 * 1024 * 1024
# Host suffixes of the object storage that serves the text files; download addresses elsewhere are refused
ALLOWED_FILE_HOST_SUFFIXES = (".cucloud.cn", ".npc.gov.cn")
SUCCESS_CODE = 200


# Fixed text in the browser challenge page the database returns when it is hit too often
CHALLENGE_MARKER = "Please enable JavaScript"


class LawSourceUnavailable(Exception):
    """The database cannot serve data right now (network failure, changed endpoint or unexpected response)."""


class LawSourceBlocked(LawSourceUnavailable):
    """The database demands a browser challenge: requests were too frequent. Stop at once and retry later; never
    work around it."""


def official_url(law_id: str, title: str) -> str:
    """Link to the official detail page."""
    return f"{BASE_URL}/detail?id={law_id}&title={quote(title)}"


def new_client() -> httpx.Client:
    return httpx.Client(headers=HEADERS, timeout=REQUEST_TIMEOUT_SECONDS, follow_redirects=True)


def _json(response: httpx.Response) -> dict:
    if response.status_code != 200:
        raise LawSourceUnavailable(f"the national database returned an error (HTTP {response.status_code})")
    try:
        return response.json()
    except ValueError as error:
        if CHALLENGE_MARKER in response.text:
            raise LawSourceBlocked("the national database demands a browser challenge (too many requests); stopped, retry later") from error
        raise LawSourceUnavailable("the national database returned a response that cannot be parsed") from error


def _request(client: httpx.Client, method: str, url: str, **kwargs) -> dict:
    try:
        return _json(client.request(method, url, **kwargs))
    except httpx.HTTPError as error:
        raise LawSourceUnavailable(f"cannot connect to the national database: {type(error).__name__}") from error


def fetch_enums(client: httpx.Client) -> dict:
    """The category tree (flfgfl) and the issuer tree (zdjgfl)."""
    payload = _request(client, "GET", ENUM_URL)
    if payload.get("code") != SUCCESS_CODE or "data" not in payload:
        raise LawSourceUnavailable("unexpected response from the enumeration endpoint of the national database")
    return payload["data"]


def fetch_list_page(client: httpx.Client, page: int, issuer_codes: list[int] | None = None) -> dict:
    """One page of the law list (newest publication first); issuer_codes filters by issuer code. Returns {total, rows}."""
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
        raise LawSourceUnavailable("unexpected response from the list endpoint of the national database")
    return payload


def fetch_detail(client: httpx.Client, law_id: str) -> dict:
    """Details of a law (metadata, text file path, revision history)."""
    payload = _request(client, "GET", DETAIL_URL, params={"bbbs": law_id})
    if payload.get("code") != SUCCESS_CODE or not payload.get("data"):
        raise LawSourceUnavailable("unexpected response from the detail endpoint of the national database")
    return payload["data"]


def fetch_docx(client: httpx.Client, law_id: str) -> bytes:
    """Download the docx file holding a law's text."""
    payload = _request(client, "GET", DOWNLOAD_URL, params={"format": "docx", "bbbs": law_id, "fileId": ""})
    file_url = (payload.get("data") or {}).get("url")
    if not file_url:
        raise LawSourceUnavailable("the national database returned no address for the text file")
    parsed = urlparse(file_url)
    if parsed.scheme != "https" or not (parsed.hostname or "").endswith(ALLOWED_FILE_HOST_SUFFIXES):
        raise LawSourceUnavailable("the text file address is outside the allowed hosts")
    try:
        response = client.get(file_url, headers={"Referer": f"{BASE_URL}/"})
    except httpx.HTTPError as error:
        raise LawSourceUnavailable(f"text file download failed: {type(error).__name__}") from error
    if response.status_code != 200:
        raise LawSourceUnavailable(f"text file download failed (HTTP {response.status_code})")
    if len(response.content) > MAX_DOCX_BYTES:
        raise LawSourceUnavailable("the text file exceeds the size limit")
    return response.content
