"""Conversion of non-standard Word files: detection, fallback without a converter, failures, real LibreOffice runs."""

import base64
import io
import shutil
import subprocess
import zipfile

import pytest
from docx import Document
from lxml import etree

from law_crawl import doc_convert
from law_crawl.docx_text import (
    REASON_CONVERSION_FAILED,
    REASON_NOT_DOCX,
    LawTextError,
    extract_paragraphs,
)
from law_crawl.repository import ContentCache

OLE2_HEADER = doc_convert.OLE2_MAGIC + b"\x00" * 64


def build_docx(paragraphs: list[str]) -> bytes:
    document = Document()
    for paragraph in paragraphs:
        document.add_paragraph(paragraph)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def to_flat_opc(docx: bytes) -> bytes:
    """Write a docx package as Word XML in the single-file form (one of the "docx" kinds the source serves)."""
    pkg = doc_convert.FLAT_OPC_NAMESPACE
    root = etree.Element(f"{{{pkg}}}package", nsmap={"pkg": pkg})
    with zipfile.ZipFile(io.BytesIO(docx)) as archive:
        declared = etree.fromstring(archive.read(doc_convert.CONTENT_TYPES_PART))
        overrides = {e.get("PartName"): e.get("ContentType") for e in declared if e.tag.endswith("Override")}
        defaults = {e.get("Extension"): e.get("ContentType") for e in declared if e.tag.endswith("Default")}
        for name in archive.namelist():
            content_type = overrides.get("/" + name) or defaults.get(name.rsplit(".", 1)[-1], "application/xml")
            part = etree.SubElement(root, f"{{{pkg}}}part", {f"{{{pkg}}}name": "/" + name, f"{{{pkg}}}contentType": content_type})
            content = archive.read(name)
            if name.endswith((".xml", ".rels")):
                etree.SubElement(part, f"{{{pkg}}}xmlData").append(etree.fromstring(content))
            else:
                etree.SubElement(part, f"{{{pkg}}}binaryData").text = base64.b64encode(content).decode()
    return b'<?xml version="1.0" standalone="yes"?>\n' + etree.tostring(root)


def with_macro_content_type(docx: bytes) -> bytes:
    """Set the body content type to macro-enabled docm, which python-docx refuses to open as is."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(docx)) as source, zipfile.ZipFile(buffer, "w") as output:
        for item in source.infolist():
            content = source.read(item.filename)
            if item.filename == doc_convert.CONTENT_TYPES_PART:
                content = content.replace(doc_convert.DOCX_MAIN_TYPE.encode(), doc_convert.NONSTANDARD_MAIN_TYPES[0].encode())
            output.writestr(item, content)
    return buffer.getvalue()


PARAGRAPHS = ["某某条例", "第一条　为了规范管理，制定本条例。", "第二条　本条例适用于全市。"]


def test_formats_are_recognized_by_their_headers():
    docx = build_docx(PARAGRAPHS)

    assert doc_convert.is_legacy_doc(b"{\\rtf1\\ansi ...")
    assert not doc_convert.is_legacy_doc(docx)
    assert doc_convert.is_flat_opc(to_flat_opc(docx))
    assert not doc_convert.is_flat_opc(docx)


def test_flat_opc_word_xml_is_parsed_without_libreoffice(monkeypatch):
    monkeypatch.setattr(doc_convert, "find_soffice", lambda: None)

    assert extract_paragraphs(to_flat_opc(build_docx(PARAGRAPHS))) == PARAGRAPHS


def test_macro_enabled_docx_is_parsed_after_fixing_its_content_type():
    macro = with_macro_content_type(build_docx(PARAGRAPHS))
    with pytest.raises(ValueError):
        Document(io.BytesIO(macro))

    assert extract_paragraphs(macro) == PARAGRAPHS


def test_flat_opc_with_a_dtd_is_rejected():
    hostile = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><pkg:package xmlns:pkg="' + doc_convert.FLAT_OPC_NAMESPACE.encode() + b'"/>'

    with pytest.raises(LawTextError, match=REASON_CONVERSION_FAILED):
        extract_paragraphs(hostile)


def test_legacy_doc_is_recognized_by_ole2_header_only():
    assert doc_convert.is_legacy_doc(OLE2_HEADER)
    assert not doc_convert.is_legacy_doc(build_docx(["第一条　内容。"]))
    assert not doc_convert.is_legacy_doc(b"")


def test_without_converter_legacy_doc_keeps_the_old_unreadable_reason(monkeypatch):
    monkeypatch.setattr(doc_convert, "find_soffice", lambda: None)

    with pytest.raises(LawTextError, match=REASON_NOT_DOCX):
        extract_paragraphs(OLE2_HEADER)


def test_converter_failure_is_reported_with_a_distinct_reason(monkeypatch):
    monkeypatch.setattr(doc_convert, "find_soffice", lambda: "/bin/false")

    with pytest.raises(LawTextError, match=REASON_CONVERSION_FAILED):
        extract_paragraphs(OLE2_HEADER)


def test_converter_timeout_is_reported_as_conversion_failure(monkeypatch):
    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("soffice", 1)

    monkeypatch.setattr(doc_convert, "find_soffice", lambda: "/bin/true")
    monkeypatch.setattr(doc_convert.subprocess, "run", timeout)

    with pytest.raises(doc_convert.DocConversionFailed, match="timed out"):
        doc_convert.doc_to_docx(OLE2_HEADER)


def test_converted_output_is_parsed_like_any_docx(monkeypatch):
    docx = build_docx(["某某条例", "第一条　为了规范管理，制定本条例。"])

    def fake_convert(data: bytes) -> bytes:
        assert doc_convert.is_legacy_doc(data)
        return docx

    monkeypatch.setattr("law_crawl.docx_text.doc_to_docx", fake_convert)

    assert extract_paragraphs(OLE2_HEADER) == ["某某条例", "第一条　为了规范管理，制定本条例。"]


def test_forget_unreadable_only_clears_the_given_reason(tmp_path):
    cache = ContentCache(tmp_path / "content.db")
    cache.mark_unreadable("a" * 32, REASON_NOT_DOCX)
    cache.mark_unreadable("b" * 32, "网页里没有找到条文")

    assert cache.forget_unreadable(REASON_NOT_DOCX) == ["a" * 32]
    assert cache.unreadable_ids() == {"b" * 32}


@pytest.mark.skipif(doc_convert.find_soffice() is None, reason="LibreOffice is not installed")
def test_libreoffice_converts_a_real_doc(tmp_path):
    """Save a docx as doc with LibreOffice, then run the whole detect -> convert -> parse path."""
    source = tmp_path / "law.docx"
    source.write_bytes(build_docx(["某某条例", "第一条　为了规范管理，制定本条例。", "第二条　本条例适用于全市。"]))
    subprocess.run(
        [doc_convert.find_soffice(), f"-env:UserInstallation={(tmp_path / 'p').as_uri()}", "--headless",
         "--convert-to", "doc:MS Word 97", "--outdir", str(tmp_path / "out"), str(source)],
        check=True, capture_output=True, timeout=120,
    )
    legacy = (tmp_path / "out" / "law.doc").read_bytes()
    assert doc_convert.is_legacy_doc(legacy)

    assert extract_paragraphs(legacy) == ["某某条例", "第一条　为了规范管理，制定本条例。", "第二条　本条例适用于全市。"]
    shutil.rmtree(tmp_path / "out")


@pytest.mark.skipif(doc_convert.find_soffice() is None, reason="LibreOffice is not installed")
def test_libreoffice_converts_a_real_rtf(tmp_path):
    source = tmp_path / "law.docx"
    source.write_bytes(build_docx(PARAGRAPHS))
    subprocess.run(
        [doc_convert.find_soffice(), f"-env:UserInstallation={(tmp_path / 'p').as_uri()}", "--headless",
         "--convert-to", "rtf", "--outdir", str(tmp_path / "out"), str(source)],
        check=True, capture_output=True, timeout=120,
    )
    rtf = (tmp_path / "out" / "law.rtf").read_bytes()
    assert doc_convert.is_legacy_doc(rtf)

    assert extract_paragraphs(rtf) == PARAGRAPHS
