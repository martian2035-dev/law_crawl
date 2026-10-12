"""Law texts: extract paragraphs from a docx and classify them as part / chapter / section headings and articles."""

from __future__ import annotations

import io
import re
import zipfile

from docx import Document

BLOCK_HEADING = "heading"
BLOCK_ARTICLE = "article"
BLOCK_TEXT = "text"
CHINESE_NUMERAL = "一二三四五六七八九十百千零〇两\\d"
HEADING_PATTERN = re.compile(rf"^第[{CHINESE_NUMERAL}]+[编分章节](\s|$)")
ARTICLE_PATTERN = re.compile(rf"^(第[{CHINESE_NUMERAL}]+条(?:之[{CHINESE_NUMERAL}]+)?)\s*")
TOC_TITLE = "目录"
IDEOGRAPHIC_SPACE = "\u3000"
LINE_BREAK_PATTERN = re.compile(r"[\n\v\f]+")


# Reasons a text cannot be parsed. They are stored verbatim in laws_content.db (unreadable.reason), so like the
# other data values they stay in Chinese.
REASON_NOT_DOCX = "正文文件不是有效的 docx"  # the text file is not a valid docx
REASON_NO_ARTICLES = "网页里没有找到条文"  # no articles found in the web page
REASON_PAGE_GONE = "规章页面不存在（HTTP 404）"  # the rule page does not exist


class LawTextError(ValueError):
    """The text file cannot be parsed."""


def normalize_paragraph(text: str) -> str:
    """Strip the ends and collapse runs of whitespace (ideographic spaces, tabs, ...) into one ideographic space,
    the separator between an article number and its text."""
    return re.sub(r"[\s\u3000]+", IDEOGRAPHIC_SPACE, text.strip())


def extract_paragraphs(docx_bytes: bytes) -> list[str]:
    """Non-empty paragraph texts of a docx, in document order, tables excluded.

    Some files put a whole chapter in one paragraph and separate the articles with soft line breaks
    (Shift+Enter), so line breaks inside a paragraph also split paragraphs."""
    try:
        document = Document(io.BytesIO(docx_bytes))
    except (zipfile.BadZipFile, KeyError, ValueError) as error:
        raise LawTextError(REASON_NOT_DOCX) from error
    lines = (line for paragraph in document.paragraphs for line in LINE_BREAK_PATTERN.split(paragraph.text))
    return [paragraph for paragraph in map(normalize_paragraph, lines) if paragraph]


def format_heading(paragraph: str) -> str:
    """Write a heading as "第一章 总则": one space between number and name, typesetting spaces inside the name removed."""
    number, _, name = paragraph.partition(IDEOGRAPHIC_SPACE)
    return f"{number} {name.replace(IDEOGRAPHIC_SPACE, '')}".strip()


def classify_blocks(paragraphs: list[str]) -> list[dict]:
    """Classify paragraphs as headings (part / chapter / section), articles and plain text; an article gets its
    number split off."""
    blocks = []
    for paragraph in paragraphs:
        article = ARTICLE_PATTERN.match(paragraph)
        if HEADING_PATTERN.match(paragraph):
            blocks.append({"type": BLOCK_HEADING, "text": format_heading(paragraph)})
        elif article:
            blocks.append(
                {"type": BLOCK_ARTICLE, "label": article.group(1), "text": paragraph[article.end():]}
            )
        else:
            blocks.append({"type": BLOCK_TEXT, "text": paragraph})
    return drop_table_of_contents(blocks)


def drop_table_of_contents(blocks: list[dict]) -> list[dict]:
    """Drop the table of contents ("目录") before the text, together with the headings it lists (they appear
    again in the text).

    The contents title is followed by a run of headings: first the chapters it lists, then the heading of the
    first chapter of the text. The table of contents ends at the first heading that repeats. When not all the
    listed headings appear later, the block is not treated as a table of contents.
    """
    for index, block in enumerate(blocks):
        if block["type"] != BLOCK_TEXT or block["text"].replace(IDEOGRAPHIC_SPACE, "") != TOC_TITLE:
            continue
        listed: list[str] = []
        end = index + 1
        while end < len(blocks) and blocks[end]["type"] == BLOCK_HEADING and blocks[end]["text"] not in listed:
            listed.append(blocks[end]["text"])
            end += 1
        later = {block["text"] for block in blocks[end:] if block["type"] == BLOCK_HEADING}
        if listed and set(listed) <= later:
            return blocks[:index] + blocks[end:]
        return blocks
    return blocks
