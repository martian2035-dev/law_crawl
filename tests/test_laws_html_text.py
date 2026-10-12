"""Tests for extracting a rule's text from its detail page (finding the text container, leaving out headers and footers, spacing after article numbers); the sample pages are made up."""

import pytest

from law_crawl.docx_text import LawTextError, classify_blocks
from law_crawl.html_text import extract_article_paragraphs

PAGE = """<html><head><style>p { color: red }</style><script>var a = "<p>第九条 脚本里的文字</p>";</script></head><body>
<div class="nav"><p>首页 &gt; 规章</p><ul><li><a href="/">返回</a></li></ul></div>
<div class="content"><div class=TRS_Editor><div class="inner">
<p align="center"><span><font>某某作业人员管理规定</font></span></p>
<p align="center"><font>（</font><font>2025年12月17日某某部令第19号公布，自2026年6月1日起施行。）</font></p>
<p>&nbsp;</p>
<p><span>第一章</span> <span>总</span> <span>则</span></p>
<p><span><font>第一条</font></span><span><font> 为了规范管理，<br>制定本规定。</font></span></p>
<p><b>第二条</b>本规定适用于下列人员：</p>
<p>（一）甲类人员；</p>
<p>第三条　本规定自2026年6月1日起施行。</p>
</div></div></div>
<div class="footer"><p>某某部发布</p><p>相关链接：</p></div>
</body></html>"""


def test_extracts_only_paragraphs_inside_the_article_container():
    paragraphs = extract_article_paragraphs(PAGE)

    assert paragraphs == [
        "某某作业人员管理规定",
        "（2025年12月17日某某部令第19号公布，自2026年6月1日起施行。）",
        "第一章　总　则",
        "第一条　为了规范管理，制定本规定。",
        "第二条　本规定适用于下列人员：",
        "（一）甲类人员；",
        "第三条　本规定自2026年6月1日起施行。",
    ]


def test_extracted_paragraphs_classify_like_docx_text():
    blocks = classify_blocks(extract_article_paragraphs(PAGE))

    assert [block["type"] for block in blocks] == ["text", "text", "heading", "article", "article", "text", "article"]
    assert blocks[2]["text"] == "第一章 总则"
    assert blocks[4] == {"type": "article", "label": "第二条", "text": "本规定适用于下列人员："}


@pytest.mark.parametrize(
    "page",
    ["<html><body><p>本页面由脚本生成内容。</p></body></html>", "<div><p>第一条 只有一条的片段</p></div>", ""],
)
def test_page_without_articles_is_rejected(page):
    with pytest.raises(LawTextError, match="没有找到条文"):
        extract_article_paragraphs(page)


def test_unclosed_tags_do_not_break_extraction():
    page = "<div><div><p>第一条 甲<p>第二条 乙<span>丙</div><p>页脚</p></div>"

    assert extract_article_paragraphs(page) == ["第一条　甲", "第二条　乙丙"]
