"""从规章详情网页里取正文段落。

各部门官网的版式不同，但正文都是一组 <p> 段落放在同一个容器里。做法：找出所有以“第 X 条”开头的段落，
取它们共同的最内层容器，容器里的全部段落就是正文（含标题、括注、章节标题和各款）。
页面里找不到条文段落时不猜，直接报错。
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

from law_crawl.docx_text import (
    ARTICLE_PATTERN,
    LawTextError,
    normalize_paragraph,
)

# 不会有结束标签的元素，不进入嵌套栈
VOID_TAGS = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"})
# 文字不属于正文的元素
SKIPPED_TAGS = frozenset({"script", "style"})
MIN_ARTICLE_PARAGRAPHS = 2


class ParagraphCollector(HTMLParser):
    """收集每个 <p> 的文字，以及它外层各级元素的编号（从外到内）。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, int]] = []
        self.next_id = 0
        self.paragraphs: list[tuple[tuple[int, ...], str]] = []
        self.current: list[str] | None = None
        self.current_path: tuple[int, ...] = ()
        self.skipping = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in VOID_TAGS:
            return
        if tag in SKIPPED_TAGS:
            self.skipping += 1
            return
        if tag == "p":
            self.close_paragraph()
            self.current = []
            self.current_path = tuple(element_id for _, element_id in self.stack)
        self.next_id += 1
        self.stack.append((tag, self.next_id))

    def handle_endtag(self, tag: str) -> None:
        if tag in SKIPPED_TAGS:
            self.skipping = max(0, self.skipping - 1)
            return
        if tag == "p":
            self.close_paragraph()
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data: str) -> None:
        if self.current is not None and not self.skipping:
            self.current.append(data)

    def close_paragraph(self) -> None:
        if self.current is not None:
            text = normalize_paragraph("".join(self.current).replace("\xa0", " "))
            if text:
                self.paragraphs.append((self.current_path, text))
            self.current = None

    def close(self) -> None:
        self.close_paragraph()
        super().close()


def common_prefix(paths: list[tuple[int, ...]]) -> tuple[int, ...]:
    prefix = paths[0]
    for path in paths[1:]:
        length = 0
        while length < min(len(prefix), len(path)) and prefix[length] == path[length]:
            length += 1
        prefix = prefix[:length]
    return prefix


def split_label(text: str) -> str:
    """“第一条为了……”补成“第一条　为了……”：网页上条号与条文之间常常没有空格。"""
    match = ARTICLE_PATTERN.match(text)
    if not match or match.end() >= len(text):
        return text
    return f"{match.group(1)}　{text[match.end():]}"


def extract_article_paragraphs(page: str) -> list[str]:
    """详情网页里的正文段落（按页面顺序）。"""
    collector = ParagraphCollector()
    collector.feed(page)
    collector.close()
    article_paths = [path for path, text in collector.paragraphs if ARTICLE_PATTERN.match(re.sub(r"^　+", "", text))]
    if len(article_paths) < MIN_ARTICLE_PARAGRAPHS:
        raise LawTextError("网页里没有找到条文")
    container = common_prefix(article_paths)
    return [split_label(text) for path, text in collector.paragraphs if path[:len(container)] == container]
