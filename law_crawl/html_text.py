"""Extract the text paragraphs from a rule's detail page.

Page layouts differ between ministry sites, but the text is always a group of <p> paragraphs inside one
container. So: find every paragraph that starts with an article number ("第 X 条"), take their innermost common
container, and treat all paragraphs in that container as the text (title, enactment note, headings, clauses).
When the page has no article paragraphs, raise instead of guessing.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

from law_crawl.docx_text import (
    ARTICLE_PATTERN,
    REASON_NO_ARTICLES,
    LawTextError,
    normalize_paragraph,
)

# Elements that never have an end tag; they are not pushed on the nesting stack
VOID_TAGS = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"})
# Elements whose text is not part of the document
SKIPPED_TAGS = frozenset({"script", "style"})
MIN_ARTICLE_PARAGRAPHS = 2


class ParagraphCollector(HTMLParser):
    """Collect the text of every <p> together with the ids of its ancestor elements, outermost first."""

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
    """Turn "第一条为了……" into "第一条　为了……": web pages often have no space between an article number and its text."""
    match = ARTICLE_PATTERN.match(text)
    if not match or match.end() >= len(text):
        return text
    return f"{match.group(1)}　{text[match.end():]}"


def extract_article_paragraphs(page: str) -> list[str]:
    """The text paragraphs of a detail page, in page order."""
    collector = ParagraphCollector()
    collector.feed(page)
    collector.close()
    article_paths = [path for path, text in collector.paragraphs if ARTICLE_PATTERN.match(re.sub(r"^　+", "", text))]
    if len(article_paths) < MIN_ARTICLE_PARAGRAPHS:
        raise LawTextError(REASON_NO_ARTICLES)
    container = common_prefix(article_paths)
    return [split_label(text) for path, text in collector.paragraphs if path[:len(container)] == container]
