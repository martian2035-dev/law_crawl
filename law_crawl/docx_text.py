"""法规正文：从 docx 提取段落，并按编、章、节、条划分段落类型。"""

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


class LawTextError(ValueError):
    """正文文件无法解析。"""


def normalize_paragraph(text: str) -> str:
    """去掉首尾空白，把全角空格、制表符等连续空白压成一个全角空格（条号与条文之间的分隔）。"""
    return re.sub(r"[\s\u3000]+", IDEOGRAPHIC_SPACE, text.strip())


def extract_paragraphs(docx_bytes: bytes) -> list[str]:
    """docx 中的非空段落文字（按文档顺序，不含表格）。

    有的文件一章只用一个段落、条与条之间用软换行（Shift+Enter）分隔，所以段内的换行也当作分段。"""
    try:
        document = Document(io.BytesIO(docx_bytes))
    except (zipfile.BadZipFile, KeyError, ValueError) as error:
        raise LawTextError("正文文件不是有效的 docx") from error
    lines = (line for paragraph in document.paragraphs for line in LINE_BREAK_PATTERN.split(paragraph.text))
    return [paragraph for paragraph in map(normalize_paragraph, lines) if paragraph]


def format_heading(paragraph: str) -> str:
    """章节标题写成“第一章 总则”：编号与名称之间一个空格，名称内部的排版空格去掉。"""
    number, _, name = paragraph.partition(IDEOGRAPHIC_SPACE)
    return f"{number} {name.replace(IDEOGRAPHIC_SPACE, '')}".strip()


def classify_blocks(paragraphs: list[str]) -> list[dict]:
    """把段落划分为标题（编章节）、条文、普通段落；条文拆出条号。"""
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
    """去掉正文前的“目录”及其列出的章节标题（这些标题在正文中会再次出现）。

    目录后面紧跟一串章节标题，其中先是目录列出的各章，随后是正文第一章的标题；
    目录到“第一个重复出现的标题”为止。目录所列标题没有全部在后文出现时不认定为目录。
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
