"""部门规章（规章库列表解析、按来源比对、正文取回、详情接口）测试，页面样例按官网结构自拟"""

import datetime as dt
import io
import json
from dataclasses import replace

import httpx
import pytest
from docx import Document

from law_crawl import ministry_rules, repository, service
from law_crawl import sync as laws_sync
from law_crawl.ministry_rules import (
    RuleSource,
    parse_note,
    parse_rule_list,
    rule_id,
)
from law_crawl.repository import ContentCache

SOURCE = RuleSource("test", "某某部", "https://rules.example.gov.cn/gzk/", 2, ministry_rules.fetch_static_rules,
                    "https://rules.example.gov.cn/gzk/index_{index}.shtml")
TODAY = dt.date(2026, 10, 13)
FLK_LAW_ID = "f" * 32

FIRST_PAGE = """
<ul class="nav"><li><a href="/index.html">首页</a></li><li><a href="/gzk/about.html">规章库说明</a></li></ul>
<div class="gz_list"><ul>
<li><div class="num">1</div><div class="title"><a href="./gz/202503/t20250327_1.shtml" target="_blank">甲类物质 管理办法</a>
<p>（2025年3月21日某某部令第38号公布，自2025年5月1日起施行）</p></div>
<div class="download"><a href="./gz/202503/P0201.docx">下载文字版</a><a href="./gz/202503/P0201.pdf">下载图片版</a></div></li>
<li><div class="num">2</div><div class="title"><a href="./gz/201801/t20180110_2.shtml">乙类设施监督管理规定</a>
<p>（2018年1月10日某某部令第3号公布，根据2022年6月1日某某部令第20号修正，自2018年3月1日起施行）</p></div></li>
</ul></div>
"""
SECOND_PAGE = """
<ul><li class="clearfix"><span class="num l">3</span><div class="title l"><p><a href="https://rules.example.gov.cn/gz/202607/t20260721_3.html"
 rel="noopener">丙类工程失信管理办法</a></p><i>（2026年7月9日某某部令2026年第15号公布 自2027年1月1日起施行）</i></div>
<div class="download r"><a href='./202607/W0203.docx' download='' target="_blank">下载文字版</a></div></li></ul>
"""


DETAIL_PAGE = """<html><body><div class="nav"><p>首页</p></div><div class="TRS_Editor">
<p>乙类设施监督管理规定</p><p>第一条 为了加强监督管理，制定本规定。</p><p>第二条 本规定自2018年3月1日起施行。</p></div>
<div class="foot"><p>某某部发布</p></div></body></html>"""


def docx_bytes(*paragraphs: str) -> bytes:
    document = Document()
    for paragraph in paragraphs:
        document.add_paragraph(paragraph)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def site_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/gzk/":
        return httpx.Response(200, text=FIRST_PAGE)
    if path == "/gzk/index_1.shtml":
        return httpx.Response(200, text=SECOND_PAGE)
    if path.endswith(".docx"):
        return httpx.Response(200, content=docx_bytes("某规章", "第一条　为了规范管理，制定本办法。"))
    if path.endswith("t20180110_2.shtml"):
        return httpx.Response(200, text=DETAIL_PAGE)
    return httpx.Response(404)


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(ministry_rules, "REQUEST_INTERVAL_SECONDS", 0)


def test_parse_rule_list_reads_title_note_and_files():
    rules = parse_rule_list(FIRST_PAGE, SOURCE.list_url)

    assert [rule["title"] for rule in rules] == ["甲类物质管理办法", "乙类设施监督管理规定"]
    assert rules[0]["url"] == "https://rules.example.gov.cn/gzk/gz/202503/t20250327_1.shtml"
    assert rules[0]["docx_url"] == "https://rules.example.gov.cn/gzk/gz/202503/P0201.docx"
    assert rules[0]["note"] == "（2025年3月21日某某部令第38号公布，自2025年5月1日起施行）"
    assert rules[1]["docx_url"] == ""


def test_parse_rule_list_accepts_issue_wording_and_unescaped_brackets():
    page = """<ul><li class="clearfix"><div class="title l"><p><a href="https://rules.example.gov.cn/gz/t1.html">丁类培训管理规定</a></p>
<i class="s">（2022年9月26日某某部发布 根据2026年2月6日《某某部关于修改<丁类培训管理规定>的决定》修正）</i></div></li>
<!--<li><div class="title l"><p><a href="">XX管理办法</a></p><i>（2020年1月1日 XX管理办法）</i></div></li>--></ul>"""

    rules = parse_rule_list(page, SOURCE.list_url)

    assert [rule["title"] for rule in rules] == ["丁类培训管理规定"]
    assert rules[0]["note"].startswith("（2022年9月26日某某部发布") and rules[0]["note"].endswith("修正）")
    assert parse_note(rules[0]["note"]) == ("2022-09-26", "")


def test_parse_rule_list_accepts_note_starting_with_order_number_and_skips_plain_brackets():
    page = """<ul><li><a href="./gz/t2.shtml">戊类船舶规则</a><p>（某某部令2016年第82号公布，自2017年3月1日起施行）</p></li>
<li><a href="./gz/about.shtml">栏目说明</a><p>（详见说明）</p></li></ul>"""

    rules = parse_rule_list(page, SOURCE.list_url)

    assert [rule["title"] for rule in rules] == ["戊类船舶规则"]
    assert parse_note(rules[0]["note"]) == ("", "2017-03-01")


def test_parse_rule_list_handles_the_second_layout():
    rules = parse_rule_list(SECOND_PAGE, "https://rules.example.gov.cn/gzk/index_1.shtml")

    assert rules == [{
        "title": "丙类工程失信管理办法",
        "url": "https://rules.example.gov.cn/gz/202607/t20260721_3.html",
        "note": "（2026年7月9日某某部令2026年第15号公布自2027年1月1日起施行）",
        "docx_url": "https://rules.example.gov.cn/gzk/202607/W0203.docx",
    }]


@pytest.mark.parametrize(
    ("note", "expected"),
    [
        ("（2025年3月21日某某部令第38号公布，自2025年5月1日起施行）", ("2025-03-21", "2025-05-01")),
        ("（2018年1月10日公布，根据2022年6月1日令第20号修正，自2018年3月1日起施行）", ("2018-01-10", "2018-03-01")),
        ("（2020年2月30日公布）", ("", "")),
        ("（某某部令第1号公布）", ("", "")),
    ],
)
def test_parse_note(note, expected):
    assert parse_note(note) == expected


def test_rule_id_is_stable_and_shaped_like_a_law_id():
    first = rule_id("https://rules.example.gov.cn/gz/1.shtml")

    assert first == rule_id("https://rules.example.gov.cn/gz/1.shtml") != rule_id("https://rules.example.gov.cn/gz/2.shtml")
    assert service.LAW_ID_PATTERN.fullmatch(first)


def test_fetch_rules_walks_pages_and_sets_status_by_effective_date():
    with httpx.Client(transport=httpx.MockTransport(site_handler)) as client:
        laws = ministry_rules.fetch_rules(client, SOURCE, TODAY)

    by_title = {law["title"]: law for law in laws}
    assert len(laws) == 3
    assert by_title["甲类物质管理办法"]["status"] == "有效"
    assert by_title["丙类工程失信管理办法"]["status"] == "尚未生效"
    assert by_title["甲类物质管理办法"]["top_category"] == "部门规章"
    assert (by_title["甲类物质管理办法"]["issuer"], by_title["甲类物质管理办法"]["category"]) == ("某某部", "某某部规章")
    assert by_title["乙类设施监督管理规定"]["publish_date"] == "2018-01-10"


def test_fetch_rules_reports_site_errors():
    with httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(503))) as client:
        with pytest.raises(ministry_rules.MinistrySourceUnavailable, match="HTTP 503"):
            ministry_rules.fetch_rules(client, SOURCE, TODAY)


@pytest.mark.parametrize("url", ["https://other.example.com/a.docx", "http://rules.example.gov.cn/a.docx"])
def test_fetch_rule_docx_only_downloads_from_the_ministry_site(url):
    with httpx.Client(transport=httpx.MockTransport(site_handler)) as client:
        with pytest.raises(ministry_rules.MinistrySourceUnavailable, match="不在该部门官网"):
            ministry_rules.fetch_rule_docx(client, url, SOURCE)


@pytest.fixture
def synced(tmp_path, monkeypatch):
    """已有一部国家法律法规数据库法规的元数据库；部门站点用模拟传输层。"""
    path = tmp_path / "laws.db"
    flk_law = {"id": FLK_LAW_ID, "title": "某法", "title_key": "某法", "top_category": "法律", "category": "法律",
               "issuer": "全国人大常委会", "region": "", "publish_date": "2020-01-01", "effective_date": "", "status": "有效"}
    repository.create_metadata_db(path, [flk_law], {"snapshot_date": "2026-10-12", "count_total": "1"})
    cache = ContentCache(tmp_path / "content.db")
    monkeypatch.setattr(repository, "METADATA_DB_PATH", path)
    monkeypatch.setattr(repository, "LAWS_DIR", tmp_path)
    monkeypatch.setattr(laws_sync, "ContentCache", lambda: cache)
    monkeypatch.setattr(service, "ContentCache", lambda: cache)
    monkeypatch.setattr(ministry_rules, "SOURCES", (SOURCE,))
    monkeypatch.setattr(ministry_rules, "new_client", lambda: httpx.Client(transport=httpx.MockTransport(site_handler)))
    return cache


def test_sync_ministries_adds_rules_without_touching_other_sources(synced):
    assert laws_sync.sync_ministries(TODAY) == 0

    laws = {law["title"]: law for law in repository.read_all_laws(repository.METADATA_DB_PATH)}
    assert set(laws) == {"某法", "甲类物质管理办法", "乙类设施监督管理规定", "丙类工程失信管理办法"}
    assert (laws["某法"]["source"], laws["甲类物质管理办法"]["source"]) == ("flk", "test")
    assert service.get_recent_changes() == []
    assert service.get_info()["total"] == 4
    # 有 docx 的两部取回了正文
    assert len(synced.cached_ids()) == 3


def test_second_sync_records_only_real_changes(synced, monkeypatch):
    laws_sync.sync_ministries(TODAY)
    monkeypatch.setattr(ministry_rules, "SOURCES", (replace(SOURCE, next_page_url="https://rules.example.gov.cn/none_{index}", min_expected=1),))

    assert laws_sync.sync_ministries(TODAY + dt.timedelta(days=7)) == 0

    changes = [(item["change_type"], item["title"]) for item in service.get_recent_changes()]
    assert changes == [("removed", "丙类工程失信管理办法")]
    assert service.search("某法")["total"] == 1


def test_sync_ministries_refuses_suspiciously_short_list(synced, monkeypatch):
    monkeypatch.setattr(ministry_rules, "SOURCES", (replace(SOURCE, min_expected=50),))

    assert laws_sync.sync_ministries(TODAY) == 1
    assert service.get_info()["total"] == 1


def test_national_sync_leaves_ministry_rules_alone(synced):
    laws_sync.sync_ministries(TODAY)
    previous = repository.read_all_laws(repository.METADATA_DB_PATH)

    merged, changes = laws_sync.merge_with_previous(previous, [], "2026-10-20")

    assert [item["change_type"] for item in changes] == ["removed"]
    assert sum(1 for law in merged if law["source"] == "test" and law["is_listed"]) == 3


def test_rule_document_uses_ministry_page_and_cached_text(synced):
    laws_sync.sync_ministries(TODAY)
    rule = service.search("甲类物质")["items"][0]

    document = service.get_document(rule["id"])

    assert document["official_url"] == "https://rules.example.gov.cn/gzk/gz/202503/t20250327_1.shtml"
    assert document["source"] == "某某部官网规章库"
    assert document["content"]["blocks"][1]["label"] == "第一条"


def test_rule_without_docx_takes_text_from_its_web_page(synced):
    laws_sync.sync_ministries(TODAY)
    rule = service.search("乙类设施")["items"][0]

    document = service.get_document(rule["id"])

    assert [block.get("label") for block in document["content"]["blocks"] if block["type"] == "article"] == ["第一条", "第二条"]
    assert all("某某部发布" not in block["text"] for block in document["content"]["blocks"])


def test_rule_whose_page_has_no_articles_is_remembered_as_unreadable(synced, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("t20180110_2.shtml"):
            return httpx.Response(200, text="<p>脚本生成的页面</p>")
        return site_handler(request)

    monkeypatch.setattr(ministry_rules, "new_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))

    laws_sync.sync_ministries(TODAY)
    rule = service.search("乙类设施")["items"][0]

    assert rule["id"] in synced.unreadable_ids()
    document = service.get_document(rule["id"])
    assert document["content"] is None and "尚未收录" in document["content_error"]


def test_search_ranks_ministry_rules_between_national_and_local(synced):
    laws_sync.sync_ministries(TODAY)

    facets = service.search("")["facets"]["categories"]

    assert {item["name"]: item["count"] for item in facets} == {"法律": 1, "部门规章": 3}


# --- 表格版式、接口类来源 ---

TABLE_PAGE = """<table><tr><th>序号</th><th class="title">标题</th><th>下载</th></tr>
<tr><td>1</td><td class="eve"><p class="bt"><a href="./202609/t20260921_1.shtml" target="_blank">己类技术服务管理规定</a></p>
<p class="xbt">（2021年9月13日某某部令第7号公布， 2026年9月18日某某部令第23号修订。）</p></td>
<td><a href="https://rules.example.gov.cn/gzk/202609/P01.wps" class="wzxz">下载文字版</a></td></tr></table>"""


def test_parse_rule_list_reads_table_rows_and_ignores_wps_files():
    rules = parse_rule_list(TABLE_PAGE, "https://rules.example.gov.cn/gzk/index.shtml")

    assert [(rule["title"], rule["docx_url"]) for rule in rules] == [("己类技术服务管理规定", "")]
    assert rules[0]["url"] == "https://rules.example.gov.cn/gzk/202609/t20260921_1.shtml"
    assert parse_note(rules[0]["note"]) == ("2021-09-13", "")


MOHURD_SOURCE = next(source for source in ministry_rules.SOURCES if source.key == "mohurd")
SAMR_SOURCE = next(source for source in ministry_rules.SOURCES if source.key == "samr")
NEA_SOURCE = next(source for source in ministry_rules.SOURCES if source.key == "nea")
MOHURD_ROW = """<tr><td class="stdn1">{number}</td><td class="stdn2">
<a href="/gongkai/zhengce/gzk/art/2026/art_{number}.html" title="规章{number}">规章{number}</a>
<p>（2026年4月20日住房和城乡建设部令第{number}号公布 自2026年9月1日起施行）</p></td>
<td class="stdn3"><a href="/api-gateway/jpaas-web-server/front/document/download?fileUrl=abc%3D&amp;fileName=%E8%A7%84%E7%AB%A0.docx">文字版</a></td></tr>"""
SAMR_ITEM = """<ul><li>{number}</li><li><div class="gzbt"><a href="/zw/zfxxgk/fdzdgknr/fgs/art/2026/art_{page_id}_{number}.html" target="_blank">办法{number}</a></div>
<div class="gzzy">（2026年5月25日国家市场监督管理总局令第{number}号公布 自2026年7月15日起施行）</div></li>
<li class="gzxz"><a href="/cms_files/attach/a{number}.doc?fileName=办法.doc">下载文字版</a><a href="/cms_files/attach/a{number}.pdf">下载pdf版</a></li></ul>"""


def unit_response(fragment: str) -> httpx.Response:
    return httpx.Response(200, json={"success": True, "data": {"html": fragment}})


def test_fetch_unit_rules_pages_through_the_content_api(monkeypatch):
    monkeypatch.setattr(ministry_rules, "UNIT_PAGE_SIZE", 2)
    requested = []

    def handler(request: httpx.Request) -> httpx.Response:
        page_number = json.loads(request.url.params["paramJson"])["pageNo"]
        requested.append((request.url.host, request.url.path, page_number, request.url.params["tagId"], request.headers["referer"]))
        rows = "".join(MOHURD_ROW.format(number=page_number * 10 + offset) for offset in range(2 if page_number == 1 else 1))
        return unit_response(f"<table><thead><tr><th>序号</th></tr></thead><tbody>{rows}</tbody></table>")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        laws = ministry_rules.fetch_rules(client, MOHURD_SOURCE, TODAY)

    assert [(host, path, page) for host, path, page, _, _ in requested] == [
        ("www.mohurd.gov.cn", ministry_rules.UNIT_API_PATH, 1), ("www.mohurd.gov.cn", ministry_rules.UNIT_API_PATH, 2),
    ]
    assert requested[0][3:] == ("内容1", MOHURD_SOURCE.list_url)
    assert [law["title"] for law in laws] == ["规章10", "规章11", "规章20"]
    assert laws[0]["source_url"] == "https://www.mohurd.gov.cn/gongkai/zhengce/gzk/art/2026/art_10.html"
    assert laws[0]["docx_url"] == (
        "https://www.mohurd.gov.cn/api-gateway/jpaas-web-server/front/document/download?fileUrl=abc%3D&fileName=%E8%A7%84%E7%AB%A0.docx"
    )
    assert (laws[0]["publish_date"], laws[0]["effective_date"], laws[0]["issuer"]) == ("2026-04-20", "2026-09-01", "住房和城乡建设部")


def test_fetch_unit_rules_reports_unexpected_response():
    with httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(200, text="<html>升级维护</html>"))) as client:
        with pytest.raises(ministry_rules.MinistrySourceUnavailable, match="无法解析"):
            ministry_rules.fetch_rules(client, MOHURD_SOURCE, TODAY)


def test_samr_rules_come_from_its_three_columns_and_use_web_pages_for_text():
    seen_pages = []

    def handler(request: httpx.Request) -> httpx.Response:
        page_id = request.url.params["pageId"]
        seen_pages.append((page_id, request.url.params["tagId"], request.headers["referer"]))
        return unit_response(SAMR_ITEM.format(number=len(seen_pages), page_id=page_id))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        laws = ministry_rules.fetch_rules(client, SAMR_SOURCE, TODAY)

    assert [page_id for page_id, _, _ in seen_pages] == [page_id for _, page_id in ministry_rules.SAMR_COLUMN_IDS]
    assert {tag for _, tag, _ in seen_pages} == {"当前内容"}
    assert seen_pages[2][2].endswith("/gz/lhzd/index.html")
    assert [law["title"] for law in laws] == ["办法1", "办法2", "办法3"]
    assert all(law["docx_url"] == "" and law["text_url"] == law["source_url"] for law in laws)
    assert laws[0]["source_url"].startswith("https://www.samr.gov.cn/zw/zfxxgk/fdzdgknr/fgs/art/2026/art_")
    assert (laws[0]["publish_date"], laws[0]["effective_date"], laws[0]["status"]) == ("2026-05-25", "2026-07-15", "有效")


NEA_PAGE = """<div id="tab_30_1" class="show"><div class="title"> 第一章 法律法规 </div><div class="list01"><ol>
<li><a href="http://www.nea.gov.cn/2021-01/01/c_1.htm" target="_blank">中华人民共和国电力法</a><span class="more">2021-01-01</span></li></ol></div></div>
<div id="tab_30_2" class="hidden"><div class="title"> 第二章 规章及规范性文件 </div><div class="list01"><ol>
<li><a href="http://zfxxgk.nea.gov.cn/auto92/201502/t20150213_1.htm" target="_blank">电力安全生产监督管理办法</a><span class="more">2017-11-02</span></li>
<li><a href="http://www.ndrc.gov.cn/xxgk/zcfb/tz/201310/t2013_2.html">国家发展改革委关于调整发电企业上网电价有关事项的通知</a><span class="more">2017-11-02</span></li>
</ol></div></div>
<div id="tab_31_2" class="hidden"><div class="title"> 第二章 规章及规范性文件 </div><div class="list01"><ol>
<li><a href="http://zfxxgk.nea.gov.cn/auto92/201502/t20150213_1.htm">电力安全生产监督管理办法</a><span class="more">2017-11-03</span></li>
<li><a href="http://zfxxgk.nea.gov.cn/auto84/201401/t2014_3.htm">煤炭经营监管办法</a><span class="more">2017-11-03</span></li></ol></div></div>"""


def test_nea_column_lists_only_the_rules_chapter_with_unverified_status():
    with httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(200, text=NEA_PAGE))) as client:
        laws = ministry_rules.fetch_rules(client, NEA_SOURCE, TODAY)

    assert [(law["title"], law["top_category"]) for law in laws] == [
        ("电力安全生产监督管理办法", "部门规章"),
        ("国家发展改革委关于调整发电企业上网电价有关事项的通知", "规范性文件"),
        ("煤炭经营监管办法", "部门规章"),
    ]
    assert {law["status"] for law in laws} == {"时效待核实"}
    assert all(law["publish_date"] == "" and law["effective_date"] == "" for law in laws)
    assert laws[0]["source_url"] == "https://zfxxgk.nea.gov.cn/auto92/201502/t20150213_1.htm"
    assert laws[1]["category"] == "国家能源局规范性文件"


@pytest.mark.parametrize(
    ("url", "allowed"),
    [
        ("https://zfxxgk.nea.gov.cn/auto92/t1.htm", True),
        ("https://www.nea.gov.cn/2021-01/01/c_1.htm", True),
        ("https://www.ndrc.gov.cn/xxgk/t2.html", False),
        ("http://zfxxgk.nea.gov.cn/auto92/t1.htm", False),
    ],
)
def test_text_is_only_fetched_from_the_source_own_hosts(url, allowed):
    assert ministry_rules.is_source_host(url, NEA_SOURCE) is allowed


def test_every_registered_source_has_a_distinct_key_and_https_list():
    keys = [source.key for source in ministry_rules.SOURCES]

    assert len(keys) == len(set(keys)) and "flk" not in keys
    assert all(source.list_url.startswith("https://") for source in ministry_rules.SOURCES)
    assert all(column.page_url.startswith("https://") for source in ministry_rules.SOURCES for column in source.columns)


def test_missing_rule_page_is_treated_as_unreadable():
    with httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(404))) as client:
        with pytest.raises(ministry_rules.LawTextError, match="不存在"):
            ministry_rules.fetch_rule_page(client, "https://rules.example.gov.cn/gz/gone.shtml", SOURCE)


def test_text_hosted_on_other_sites_is_not_requested(tmp_path):
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(200, text=DETAIL_PAGE)

    rules = [{"id": "1" * 32, "title": "外站文件", "docx_url": "", "text_url": "https://www.ndrc.gov.cn/xxgk/t1.html"},
             {"id": "2" * 32, "title": "本站文件", "docx_url": "", "text_url": "https://zfxxgk.nea.gov.cn/auto92/t2.htm"}]
    cache = ContentCache(tmp_path / "content.db")
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        fetched = laws_sync.fetch_ministry_contents(client, NEA_SOURCE, rules, cache)

    assert fetched == 1 and requests == ["https://zfxxgk.nea.gov.cn/auto92/t2.htm"]
