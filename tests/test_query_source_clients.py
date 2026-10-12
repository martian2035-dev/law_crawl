"""Tests for the national database client and bulk text fetching; every network request goes through a mock transport."""

import io

import httpx
import pytest
from docx import Document

from law_crawl import flk_client, repository
from law_crawl import sync as laws_sync
from law_crawl.repository import ContentCache

LAW_ID = "a" * 32
FILE_URL = "https://flkoss.obs-bj2.cucloud.cn/prod/law.docx"


def docx_bytes(text: str) -> bytes:
    document = Document()
    document.add_paragraph(text)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


# --- National database client ---

def flk(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_fetch_enums_returns_data():
    with flk(lambda _request: httpx.Response(200, json={"code": 200, "data": {"flfgfl": {}}})) as client:
        assert flk_client.fetch_enums(client) == {"flfgfl": {}}


def test_fetch_enums_rejects_error_code():
    with flk(lambda _request: httpx.Response(200, json={"code": 500, "msg": "error"})) as client:
        with pytest.raises(flk_client.LawSourceUnavailable, match="enumeration"):
            flk_client.fetch_enums(client)


def test_fetch_list_page_posts_filters():
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.read())
        return httpx.Response(200, json={"total": 1, "rows": [{"bbbs": LAW_ID}]})

    with flk(handler) as client:
        payload = flk_client.fetch_list_page(client, 3, issuer_codes=[300])

    assert payload["rows"] == [{"bbbs": LAW_ID}]
    assert b'"pageNum":3' in bodies[0] and b'"zdjgCodeId":[300]' in bodies[0]


@pytest.mark.parametrize(
    "response",
    [httpx.Response(503), httpx.Response(200, text="not json"), httpx.Response(200, json={"msg": "no rows"})],
)
def test_fetch_list_page_reports_source_problems(response):
    with flk(lambda _request: response) as client, pytest.raises(flk_client.LawSourceUnavailable):
        flk_client.fetch_list_page(client, 1)


def test_fetch_detail_returns_data():
    with flk(lambda _request: httpx.Response(200, json={"code": 200, "data": {"title": "安全生产法"}})) as client:
        assert flk_client.fetch_detail(client, LAW_ID) == {"title": "安全生产法"}


def docx_handler(file_url: str | None, body: bytes = b"docx"):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "flk.npc.gov.cn":
            return httpx.Response(200, json={"code": 200, "data": {"url": file_url}})
        return httpx.Response(200, content=body)

    return handler


def test_fetch_docx_downloads_file_from_storage():
    with flk(docx_handler(FILE_URL, b"file-content")) as client:
        assert flk_client.fetch_docx(client, LAW_ID) == b"file-content"


@pytest.mark.parametrize(
    ("file_url", "message"),
    [
        (None, "no address for the text file"),
        ("http://flkoss.obs-bj2.cucloud.cn/prod/law.docx", "outside the allowed hosts"),
        ("https://169.254.169.254/latest/meta-data", "outside the allowed hosts"),
        ("https://evil.example.com/law.docx", "outside the allowed hosts"),
    ],
)
def test_fetch_docx_rejects_missing_or_untrusted_file_url(file_url, message):
    with flk(docx_handler(file_url)) as client, pytest.raises(flk_client.LawSourceUnavailable, match=message):
        flk_client.fetch_docx(client, LAW_ID)


def test_fetch_docx_rejects_oversized_file(monkeypatch):
    monkeypatch.setattr(flk_client, "MAX_DOCX_BYTES", 3)

    with flk(docx_handler(FILE_URL, b"too large")) as client, pytest.raises(flk_client.LawSourceUnavailable, match="size limit"):
        flk_client.fetch_docx(client, LAW_ID)


# --- Bulk text fetching ---

@pytest.fixture
def prefetch_env(tmp_path, monkeypatch):
    """A temporary catalogue database with three laws and an empty text cache; fetching neither waits nor touches the network."""
    laws = [
        {"id": "c" * 32, "title": "某市条例", "top_category": "地方法规", "status": "有效"},
        {"id": "b" * 32, "title": "旧法", "top_category": "法律", "status": "已废止"},
        {"id": LAW_ID, "title": "安全生产法", "top_category": "法律", "status": "有效"},
    ]
    rows = [
        {**law, "title_key": law["title"], "category": law["top_category"], "issuer": "", "region": "",
         "publish_date": "", "effective_date": ""}
        for law in laws
    ]
    repository.create_metadata_db(tmp_path / "laws.db", rows, {"count_total": "3"})
    monkeypatch.setattr(repository, "METADATA_DB_PATH", tmp_path / "laws.db")
    cache = ContentCache(tmp_path / "content.db")
    monkeypatch.setattr(laws_sync, "ContentCache", lambda: cache)
    monkeypatch.setattr(laws_sync, "CONTENT_INTERVAL_SECONDS", 0)
    return cache


def test_pending_content_orders_national_and_current_first(prefetch_env):
    assert laws_sync.pending_content_ids(prefetch_env, None) == [LAW_ID, "b" * 32, "c" * 32]
    assert laws_sync.pending_content_ids(prefetch_env, "地方法规") == ["c" * 32]


def test_sync_content_respects_limit_and_resumes(prefetch_env, monkeypatch):
    fetched = []

    def fake_fetch(_client, law_id):
        fetched.append(law_id)
        return docx_bytes("第一条　内容。")

    monkeypatch.setattr(laws_sync.flk_client, "fetch_docx", fake_fetch)

    assert laws_sync.sync_content(limit=2, budget_seconds=60, category=None) == 0
    assert laws_sync.sync_content(limit=2, budget_seconds=60, category=None) == 0

    assert fetched == [LAW_ID, "b" * 32, "c" * 32]
    assert prefetch_env.get(LAW_ID)["paragraphs"] == ["第一条　内容。"]


def test_sync_content_stops_after_consecutive_failures(prefetch_env, monkeypatch):
    def failing(_client, _law_id):
        raise flk_client.LawSourceUnavailable("cannot connect to the national database: ConnectError")

    monkeypatch.setattr(laws_sync.flk_client, "fetch_docx", failing)
    monkeypatch.setattr(laws_sync, "MAX_CONSECUTIVE_FAILURES", 1)
    monkeypatch.setattr(laws_sync, "MAX_FAILURE_PAUSES", 1)
    monkeypatch.setattr(laws_sync, "FAILURE_PAUSE_SECONDS", 0)

    assert laws_sync.sync_content(limit=10, budget_seconds=60, category=None) == 1
    assert prefetch_env.cached_ids() == set()


def test_sync_content_pauses_and_recovers_from_a_temporary_outage(prefetch_env, monkeypatch):
    calls = []
    pauses = []

    def flaky(_client, law_id):
        calls.append(law_id)
        if len(calls) <= 2:
            raise flk_client.LawSourceUnavailable("cannot connect to the national database: ProxyError")
        return docx_bytes("第一条\u3000内容。")

    monkeypatch.setattr(laws_sync.flk_client, "fetch_docx", flaky)
    monkeypatch.setattr(laws_sync, "MAX_CONSECUTIVE_FAILURES", 2)
    monkeypatch.setattr(laws_sync, "FAILURE_PAUSE_SECONDS", 7)
    monkeypatch.setattr(laws_sync.time, "sleep", pauses.append)

    assert laws_sync.sync_content(limit=10, budget_seconds=60, category=None) == 0
    assert 7 in pauses
    assert len(prefetch_env.cached_ids()) == 1


def test_sync_metadata_keeps_existing_database_when_too_few_laws(tmp_path, monkeypatch):
    monkeypatch.setattr(repository, "METADATA_DB_PATH", tmp_path / "laws.db")
    monkeypatch.setattr(laws_sync, "collect_laws", lambda _client: [])

    assert laws_sync.sync_metadata() == 1
    assert not (tmp_path / "laws.db").exists()
