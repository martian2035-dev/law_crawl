"""Tests for law data (text parsing, title search, text cache, sync)."""

import io

import pytest
from docx import Document

from law_crawl import flk_client, repository, service
from law_crawl import sync as laws_sync
from law_crawl.docx_text import (
    LawTextError,
    classify_blocks,
    extract_paragraphs,
)
from law_crawl.repository import ContentCache

SAFETY_LAW_ID = "a" * 32
LOCAL_LAW_ID = "b" * 32
OLD_LAW_ID = "c" * 32
DECISION_ID = "d" * 32


def build_docx(paragraphs: list[str]) -> bytes:
    document = Document()
    for paragraph in paragraphs:
        document.add_paragraph(paragraph)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def make_law(law_id: str, title: str, **overrides) -> dict:
    law = {
        "id": law_id,
        "title": title,
        "title_key": repository.title_key(title),
        "top_category": "法律",
        "category": "社会法",
        "issuer": "全国人民代表大会常务委员会",
        "region": "",
        "publish_date": "2021-06-10",
        "effective_date": "2021-09-01",
        "status": repository.STATUS_EFFECTIVE,
    }
    return {**law, **overrides}


@pytest.fixture
def sample_db(tmp_path, monkeypatch):
    """A temporary catalogue database with four laws, which the service layer is pointed at."""
    path = tmp_path / "laws.db"
    laws = [
        make_law(SAFETY_LAW_ID, "中华人民共和国安全生产法"),
        make_law(LOCAL_LAW_ID, "山东省安全生产条例", top_category="地方法规", category="地方性法规",
                 issuer="山东省人民代表大会常务委员会", region="山东", publish_date="2021-12-03"),
        make_law(OLD_LAW_ID, "中华人民共和国安全生产法", status=repository.STATUS_AMENDED, publish_date="2014-08-31"),
        make_law(DECISION_ID, "全国人民代表大会常务委员会关于修改《中华人民共和国安全生产法》的决定",
                 category="修改、废止的决定", status=""),
    ]
    repository.create_metadata_db(path, laws, {"snapshot_date": "2026-10-12", "count_total": "4"})
    monkeypatch.setattr(repository, "METADATA_DB_PATH", path)
    return path


# --- Text parsing ---

def test_extract_paragraphs_skips_blank_and_normalizes_spaces():
    docx = build_docx(["  中华人民共和国安全生产法 ", "", "第一条　　为了加强安全生产工作，制定本法。"])

    assert extract_paragraphs(docx) == ["中华人民共和国安全生产法", "第一条　为了加强安全生产工作，制定本法。"]


def test_extract_paragraphs_splits_soft_line_breaks():
    document = Document()
    run = document.add_paragraph().add_run("第一条\u3000甲。")
    run.add_break()
    run.add_text("第二条\u3000乙。")
    run.add_break()
    run.add_text("（一）丙。")
    buffer = io.BytesIO()
    document.save(buffer)

    assert extract_paragraphs(buffer.getvalue()) == ["第一条\u3000甲。", "第二条\u3000乙。", "（一）丙。"]


def test_extract_paragraphs_rejects_invalid_file():
    with pytest.raises(LawTextError):
        extract_paragraphs(b"not a docx")


def test_classify_blocks_splits_headings_articles_and_text():
    blocks = classify_blocks(
        ["安全生产法", "第一章　总　则", "第一条　为了加强安全生产工作。", "生产经营单位应当遵守本法。", "第十条之一　补充规定。"]
    )

    assert blocks == [
        {"type": "text", "text": "安全生产法"},
        {"type": "heading", "text": "第一章 总则"},
        {"type": "article", "label": "第一条", "text": "为了加强安全生产工作。"},
        {"type": "text", "text": "生产经营单位应当遵守本法。"},
        {"type": "article", "label": "第十条之一", "text": "补充规定。"},
    ]


def test_classify_blocks_does_not_treat_reference_as_article():
    blocks = classify_blocks(["依照第三条的规定处理。"])

    assert blocks == [{"type": "text", "text": "依照第三条的规定处理。"}]


def test_classify_blocks_drops_table_of_contents():
    blocks = classify_blocks(
        ["安全生产法", "目　录", "第一章　总则", "第二章　附则", "第一章　总则", "第一条　内容。", "第二章　附则"]
    )

    assert [block["text"] for block in blocks if block["type"] == "heading"] == ["第一章 总则", "第二章 附则"]
    assert all(block["text"] != "目　录" for block in blocks)


# --- Title search ---

def test_title_key_ignores_punctuation_and_width():
    assert repository.title_key("《中华人民共和国 安全生产法》（２０２１）") == "中华人民共和国安全生产法2021"


def test_search_defaults_to_current_laws(sample_db):
    result = service.search("安全生产")

    # laws rank before local regulations
    assert [item["id"] for item in result["items"]] == [SAFETY_LAW_ID, LOCAL_LAW_ID]
    assert result["total"] == 2


def test_search_scope_all_includes_amended_and_decisions(sample_db):
    result = service.search("安全生产法", status_scope=repository.STATUS_SCOPE_ALL)

    assert result["total"] == 3
    assert {item["status"] for item in result["items"]} == {"有效", "已修改", ""}


def test_search_requires_all_keywords(sample_db):
    assert [item["id"] for item in service.search("山东 条例")["items"]] == [LOCAL_LAW_ID]
    assert service.search("山东 法律责任")["total"] == 0


def test_search_filters_by_category_and_region(sample_db):
    assert service.search("", category="地方法规", region="山东")["total"] == 1
    assert service.search("", category="地方法规", region="河北")["total"] == 0


def test_search_reports_facets(sample_db):
    facets = service.search("安全生产", category="地方法规")["facets"]

    assert {item["name"]: item["count"] for item in facets["categories"]} == {"法律": 1, "地方法规": 1}
    assert facets["regions"] == [{"name": "山东", "count": 1}]


@pytest.mark.parametrize("query", ["%", "100%", "安全_生产%", "'; DROP TABLE laws; --"])
def test_search_treats_wildcards_literally(sample_db, query):
    assert service.search(query)["total"] == 0


def test_search_with_only_punctuation_browses_all_current_laws(sample_db):
    assert service.search("《_》")["total"] == 2


def test_search_paginates(sample_db):
    first = service.search("", status_scope=repository.STATUS_SCOPE_ALL, page=1, page_size=3)
    second = service.search("", status_scope=repository.STATUS_SCOPE_ALL, page=2, page_size=3)

    assert len(first["items"]) == 3 and len(second["items"]) == 1


def test_info_reports_snapshot(sample_db):
    info = service.get_info()

    assert info["snapshot_date"] == "2026-10-12"
    assert info["total"] == 4
    assert "国家法律法规数据库" in info["source"]


# --- Law texts ---

class FakeDocxFetcher:
    def __init__(self, paragraphs: list[str]) -> None:
        self.docx = build_docx(paragraphs)
        self.calls: list[str] = []

    def __call__(self, _client, law_id: str) -> bytes:
        self.calls.append(law_id)
        return self.docx


def test_get_document_fetches_parses_and_caches(sample_db, tmp_path):
    cache = ContentCache(tmp_path / "content.db")
    fetcher = FakeDocxFetcher(["中华人民共和国安全生产法", "第一条　为了加强安全生产工作。"])

    first = service.get_document(SAFETY_LAW_ID, cache=cache, fetch_docx=fetcher)
    second = service.get_document(SAFETY_LAW_ID, cache=cache, fetch_docx=fetcher)

    assert fetcher.calls == [SAFETY_LAW_ID]
    assert first["title"] == "中华人民共和国安全生产法"
    assert first["content"]["blocks"][1] == {"type": "article", "label": "第一条", "text": "为了加强安全生产工作。"}
    assert second["content"]["blocks"] == first["content"]["blocks"]
    assert first["official_url"].startswith("https://flk.npc.gov.cn/detail?id=" + SAFETY_LAW_ID)


def test_get_document_reports_unreadable_file(sample_db, tmp_path):
    document = service.get_document(
        SAFETY_LAW_ID, cache=ContentCache(tmp_path / "content.db"), fetch_docx=lambda _client, _law_id: b"broken"
    )

    assert document["content"] is None
    assert "docx" in document["content_error"]


@pytest.mark.parametrize("law_id", ["e" * 32, "../etc/passwd", "A" * 32, ""])
def test_get_document_rejects_unknown_or_malformed_id(sample_db, law_id):
    with pytest.raises(service.LawNotFound):
        service.get_document(law_id)


def test_fetches_from_source_are_spaced_out(sample_db, tmp_path, monkeypatch):
    waits: list[float] = []
    monkeypatch.setattr(service.time, "sleep", waits.append)
    monkeypatch.setattr(service, "_last_fetch_at", service.time.monotonic())
    fetcher = FakeDocxFetcher(["山东省安全生产条例"])

    service.get_document(LOCAL_LAW_ID, cache=ContentCache(tmp_path / "content.db"), fetch_docx=fetcher)

    assert len(waits) == 1 and 0 < waits[0] <= service.MIN_FETCH_INTERVAL_SECONDS


# --- Sync ---

CATEGORY_TREE = {
    "children": [
        {"name": "法律", "codeId": 101, "children": [{"name": "法律", "codeId": 102, "children": [
            {"name": "社会法", "codeId": 150, "children": []}]}]},
        {"name": "地方法规", "codeId": 221, "children": [{"name": "地方性法规", "codeId": 230, "children": []}]},
    ]
}
ISSUER_TREE = {
    "children": [
        {"name": "国务院", "codeId": 120, "children": []},
        {"name": "地方人大及其常委会", "codeId": 165, "children": [{"name": "山东", "codeId": 300, "children": []}]},
    ]
}


def list_row(law_id: str, title: str, code: int, issuer: str, status: int | None = 3) -> dict:
    return {"bbbs": law_id, "title": title, "flfgCodeId": code, "flxz": "", "zdjgName": issuer,
            "gbrq": "2021-06-10", "sxrq": "2021-09-01", "sxx": status}


def test_category_names_maps_codes_to_top_category():
    names = laws_sync.category_names(CATEGORY_TREE)

    assert names[150] == ("法律", "社会法")
    assert names[230] == ("地方法规", "地方性法规")


def test_province_issuers_lists_local_groups():
    assert laws_sync.province_issuers(ISSUER_TREE) == [(300, "山东")]


def test_region_by_issuer_matches_city_and_autonomous_county():
    places = laws_sync.place_index(["山东", "内蒙古", "青海"])

    assert laws_sync.region_by_issuer("青岛市人民代表大会常务委员会", places) == "山东"
    assert laws_sync.region_by_issuer("海北藏族自治州人大", places) == "青海"
    assert laws_sync.region_by_issuer("内蒙古自治区人民代表大会常务委员会", places) == "内蒙古"
    assert laws_sync.region_by_issuer("全国人民代表大会常务委员会", places) == ""


def test_collect_laws_assigns_region_and_status(monkeypatch):
    national = list_row(SAFETY_LAW_ID, "中华人民共和国安全生产法", 150, "全国人民代表大会常务委员会")
    provincial = list_row(LOCAL_LAW_ID, "山东省安全生产条例", 230, "山东省人民代表大会常务委员会", status=2)
    city = list_row(OLD_LAW_ID, "青岛市安全生产条例", 230, "青岛市人民代表大会常务委员会", status=None)
    monkeypatch.setattr(laws_sync.flk_client, "fetch_enums", lambda _client: {"flfgfl": CATEGORY_TREE, "zdjgfl": ISSUER_TREE})
    monkeypatch.setattr(
        laws_sync.flk_client,
        "fetch_list_page",
        lambda _client, _page, issuer_codes=None: (
            {"total": 1, "rows": [provincial]} if issuer_codes == [300] else {"total": 3, "rows": [national, provincial, city]}
        ),
    )
    monkeypatch.setattr(laws_sync, "REQUEST_INTERVAL_SECONDS", 0)

    laws = {law["id"]: law for law in laws_sync.collect_laws(client=None)}

    assert laws[SAFETY_LAW_ID]["top_category"] == "法律" and laws[SAFETY_LAW_ID]["region"] == ""
    assert laws[LOCAL_LAW_ID]["region"] == "山东" and laws[LOCAL_LAW_ID]["status"] == "已修改"
    assert laws[OLD_LAW_ID]["region"] == "山东" and laws[OLD_LAW_ID]["status"] == ""


# --- Incremental sync ---

def test_merge_first_build_records_no_additions():
    merged, changes = laws_sync.merge_with_previous([], [make_law(SAFETY_LAW_ID, "安全生产法")], "2026-10-13")

    assert changes == []
    assert [law["is_listed"] for law in merged] == [1]


def test_merge_detects_added_status_removed_and_restored():
    previous = [
        {**make_law(SAFETY_LAW_ID, "安全生产法"), "is_listed": 1},
        {**make_law(OLD_LAW_ID, "旧条例"), "is_listed": 1},
        {**make_law(DECISION_ID, "曾下架的条例"), "is_listed": 0},
    ]
    current = [
        make_law(SAFETY_LAW_ID, "安全生产法", status=repository.STATUS_AMENDED),
        make_law(LOCAL_LAW_ID, "新条例", status=repository.STATUS_PENDING),
        make_law(DECISION_ID, "曾下架的条例"),
    ]

    merged, changes = laws_sync.merge_with_previous(previous, current, "2026-10-13")

    summary = {(item["law_id"], item["change_type"]): (item["old_value"], item["new_value"]) for item in changes}
    assert summary == {
        (SAFETY_LAW_ID, "status"): ("有效", "已修改"),
        (LOCAL_LAW_ID, "added"): ("", "尚未生效"),
        (DECISION_ID, "restored"): ("", "有效"),
        (OLD_LAW_ID, "removed"): ("有效", ""),
    }
    assert all(item["detected_on"] == "2026-10-13" for item in changes)
    assert {law["id"]: law["is_listed"] for law in merged} == {
        SAFETY_LAW_ID: 1, LOCAL_LAW_ID: 1, DECISION_ID: 1, OLD_LAW_ID: 0,
    }


def test_merge_does_not_repeat_removed_for_already_unlisted_law():
    previous = [{**make_law(OLD_LAW_ID, "旧条例"), "is_listed": 0}, {**make_law(SAFETY_LAW_ID, "安全生产法"), "is_listed": 1}]

    _, changes = laws_sync.merge_with_previous(previous, [make_law(SAFETY_LAW_ID, "安全生产法")], "2026-10-13")

    assert changes == []


@pytest.fixture
def sync_env(tmp_path, monkeypatch):
    """Make the catalogue sync write to a temporary database and accept a very small catalogue."""
    monkeypatch.setattr(repository, "METADATA_DB_PATH", tmp_path / "laws.db")
    monkeypatch.setattr(laws_sync, "MIN_EXPECTED_LAWS", 1)
    monkeypatch.setattr(laws_sync, "MAX_REMOVED_RATIO", 0.5)
    return tmp_path / "laws.db"


def run_metadata_sync(monkeypatch, laws, day):
    import datetime as dt

    monkeypatch.setattr(laws_sync, "collect_laws", lambda _client: laws)
    return laws_sync.sync_metadata(today=dt.date.fromisoformat(day))


def test_sync_metadata_accumulates_changes_and_keeps_unlisted_laws(sync_env, monkeypatch):
    first = [make_law(SAFETY_LAW_ID, "安全生产法"), make_law(OLD_LAW_ID, "旧条例"), make_law(DECISION_ID, "某决定")]
    second = [make_law(SAFETY_LAW_ID, "安全生产法", status=repository.STATUS_AMENDED), make_law(DECISION_ID, "某决定"),
              make_law(LOCAL_LAW_ID, "新条例")]

    assert run_metadata_sync(monkeypatch, first, "2026-10-06") == 0
    assert run_metadata_sync(monkeypatch, second, "2026-10-13") == 0
    assert run_metadata_sync(monkeypatch, second, "2026-10-20") == 0

    changes = service.get_recent_changes()
    assert sorted((item["change_type"], item["title"], item["detected_on"]) for item in changes) == [
        ("added", "新条例", "2026-10-13"), ("removed", "旧条例", "2026-10-13"), ("status", "安全生产法", "2026-10-13"),
    ]
    assert service.get_info()["total"] == 3
    assert service.search("旧条例")["total"] == 0
    unlisted = service.search("旧条例", status_scope=repository.STATUS_SCOPE_ALL)["items"]
    assert [(item["title"], item["is_listed"]) for item in unlisted] == [("旧条例", 0)]


def test_sync_metadata_refuses_mass_removal(sync_env, monkeypatch):
    first = [make_law(SAFETY_LAW_ID, "安全生产法"), make_law(OLD_LAW_ID, "旧条例"), make_law(DECISION_ID, "某决定")]
    assert run_metadata_sync(monkeypatch, first, "2026-10-06") == 0

    assert run_metadata_sync(monkeypatch, [make_law(SAFETY_LAW_ID, "安全生产法")], "2026-10-13") == 1

    assert service.get_info()["total"] == 3
    assert service.get_recent_changes() == []


def test_sync_metadata_upgrades_database_without_listing_column(sync_env, monkeypatch):
    import sqlite3

    connection = sqlite3.connect(sync_env)
    connection.executescript(
        "CREATE TABLE laws (id TEXT PRIMARY KEY, title TEXT, title_key TEXT, top_category TEXT, category TEXT, issuer TEXT, "
        "region TEXT, publish_date TEXT, effective_date TEXT, status TEXT); CREATE TABLE meta (key TEXT, value TEXT);"
    )
    connection.execute("INSERT INTO laws VALUES (?, '安全生产法', '安全生产法', '法律', '社会法', '', '', '', '', '有效')", (SAFETY_LAW_ID,))
    connection.commit()
    connection.close()

    assert run_metadata_sync(monkeypatch, [make_law(SAFETY_LAW_ID, "安全生产法"), make_law(LOCAL_LAW_ID, "新条例")], "2026-10-13") == 0

    assert [(item["change_type"], item["title"]) for item in service.get_recent_changes()] == [("added", "新条例")]


def test_sync_content_records_unreadable_files_and_skips_them_next_time(sample_db, tmp_path, monkeypatch):
    cache = ContentCache(tmp_path / "content.db")
    calls = []

    def fake_fetch(_client, law_id):
        calls.append(law_id)
        return b"old binary doc" if law_id == SAFETY_LAW_ID else build_docx(["第一条\u3000内容。"])

    monkeypatch.setattr(laws_sync, "ContentCache", lambda: cache)
    monkeypatch.setattr(laws_sync, "CONTENT_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(laws_sync, "MAX_CONSECUTIVE_FAILURES", 1)
    monkeypatch.setattr(laws_sync.flk_client, "fetch_docx", fake_fetch)

    assert laws_sync.sync_content(limit=10, budget_seconds=60, category=None) == 0
    assert laws_sync.sync_content(limit=10, budget_seconds=60, category=None) == 0

    assert len(calls) == 4 and calls.count(SAFETY_LAW_ID) == 1
    assert cache.unreadable_ids() == {SAFETY_LAW_ID}
    assert cache.cached_ids() == {LOCAL_LAW_ID, OLD_LAW_ID, DECISION_ID}


# --- Rate limiting and concurrency guards ---


def test_only_one_sync_process_runs_at_a_time(tmp_path, monkeypatch):
    monkeypatch.setattr(repository, "LAWS_DIR", tmp_path)

    with laws_sync.single_instance(), pytest.raises(laws_sync.AlreadyRunning), laws_sync.single_instance():
        pass
    with laws_sync.single_instance():
        pass


def test_national_and_ministry_locks_are_independent(tmp_path, monkeypatch):
    monkeypatch.setattr(repository, "LAWS_DIR", tmp_path)

    with laws_sync.single_instance(laws_sync.LOCK_NATIONAL), laws_sync.single_instance(laws_sync.LOCK_MINISTRIES):
        with pytest.raises(laws_sync.AlreadyRunning):
            with laws_sync.single_instance(laws_sync.LOCK_MINISTRIES):
                pass


def test_update_runs_every_step_even_when_an_earlier_one_fails(tmp_path, monkeypatch):
    steps = []
    monkeypatch.setattr(laws_sync, "sync_metadata", lambda: steps.append("metadata") or 0)
    monkeypatch.setattr(laws_sync, "sync_content", lambda limit, budget, category, retry=False: steps.append(("content", limit)) or 0)
    monkeypatch.setattr(laws_sync, "sync_ministries", lambda: steps.append("ministries") or 0)
    monkeypatch.setattr(repository, "LAWS_DIR", tmp_path)

    assert laws_sync.update(content_limit=50, budget_seconds=60) == 0
    assert steps == ["metadata", ("content", 50), "ministries"]

    steps.clear()
    monkeypatch.setattr(laws_sync, "sync_metadata", lambda: steps.append("metadata") or 1)
    assert laws_sync.update(content_limit=50, budget_seconds=60) == 1
    assert steps == ["metadata", "ministries"]


def test_update_reports_blocked_source_but_still_syncs_ministries(tmp_path, monkeypatch):
    steps = []
    monkeypatch.setattr(repository, "LAWS_DIR", tmp_path)

    def blocked():
        raise flk_client.LawSourceBlocked("官方数据库要求浏览器验证")

    monkeypatch.setattr(laws_sync, "sync_metadata", blocked)
    monkeypatch.setattr(laws_sync, "sync_ministries", lambda: steps.append("ministries") or 0)

    assert laws_sync.update(content_limit=50, budget_seconds=60) == laws_sync.EXIT_BLOCKED
    assert steps == ["ministries"]


def test_update_skips_national_step_while_a_download_is_running(tmp_path, monkeypatch):
    steps = []
    monkeypatch.setattr(repository, "LAWS_DIR", tmp_path)
    monkeypatch.setattr(laws_sync, "sync_metadata", lambda: steps.append("metadata") or 0)
    monkeypatch.setattr(laws_sync, "sync_ministries", lambda: steps.append("ministries") or 0)

    with laws_sync.single_instance(laws_sync.LOCK_NATIONAL):
        assert laws_sync.update(content_limit=50, budget_seconds=60) == 0

    assert steps == ["ministries"]


def test_manifest_summarizes_sources_content_and_changes(sync_env, tmp_path, monkeypatch):
    cache = ContentCache(tmp_path / "content.db")
    monkeypatch.setattr(laws_sync, "ContentCache", lambda: cache)
    run_metadata_sync(monkeypatch, [make_law(SAFETY_LAW_ID, "安全生产法")], "2026-10-06")
    run_metadata_sync(monkeypatch, [make_law(SAFETY_LAW_ID, "安全生产法"), make_law(LOCAL_LAW_ID, "新条例")], "2026-10-13")
    cache.put(SAFETY_LAW_ID, ["第一条\u3000内容。"])

    manifest = laws_sync.build_manifest()

    assert (manifest["snapshot_date"], manifest["laws"], manifest["listed"], manifest["with_content"]) == ("2026-10-13", 2, 2, 1)
    assert manifest["by_source"] == {"flk": 2}
    assert [item["title"] for item in manifest["recent_changes"]] == ["新条例"]


def test_manifest_without_database_is_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(repository, "METADATA_DB_PATH", tmp_path / "missing.db")

    assert laws_sync.build_manifest() == {"laws": 0, "with_content": 0}


def test_probe_reports_unreachable_sources(monkeypatch):
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "flk.npc.gov.cn":
            return httpx.Response(200, json={"code": 200, "data": {}})
        return httpx.Response(200 if request.url.host != "www.mee.gov.cn" else 403)

    def client_factory() -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(laws_sync.flk_client, "new_client", client_factory)
    monkeypatch.setattr(laws_sync.ministry_rules, "new_client", client_factory)

    assert laws_sync.probe() == 1


def test_challenge_page_is_reported_as_blocked():
    import httpx

    page = "<html><noscript><h1>Please enable JavaScript and refresh the page.</h1></noscript></html>"
    with httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(200, text=page))) as http:
        with pytest.raises(flk_client.LawSourceBlocked, match="browser challenge"):
            flk_client.fetch_enums(http)


def test_sync_content_stops_at_once_when_blocked(sample_db, tmp_path, monkeypatch):
    calls = []

    def blocked(_client, law_id):
        calls.append(law_id)
        raise flk_client.LawSourceBlocked("官方数据库要求浏览器验证")

    monkeypatch.setattr(laws_sync, "ContentCache", lambda: ContentCache(tmp_path / "content.db"))
    monkeypatch.setattr(laws_sync, "CONTENT_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(laws_sync.flk_client, "fetch_docx", blocked)

    with pytest.raises(flk_client.LawSourceBlocked):
        laws_sync.sync_content(limit=10, budget_seconds=60, category=None)
    assert len(calls) == 1
