"""Convert non-standard Word files to a standard docx.

The "docx" offered by the national database is not always a docx, and its file extension cannot be trusted. Four
cases occur in practice:

- legacy Word (OLE2; mostly local regulations of 2019-2022) and RTF (often named .doc): converted with LibreOffice
  in headless mode;
- Word XML in the single-file form (Flat OPC, `<pkg:package>`; mostly Hainan): rebuilt into a docx package in
  Python, no LibreOffice needed;
- macro-enabled Word (macroEnabled content type): python-docx refuses it, so the content type is corrected.

The converter (soffice) is installed only where it is needed: the crawl workflow installs it. Where it is missing,
DocConversionUnavailable is raised and the caller records the text as unreadable, exactly as before conversion was
supported; a later run with `--retry-unreadable` fetches it again once LibreOffice is available.
"""

from __future__ import annotations

import base64
import io
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

from lxml import etree

# File headers of legacy Office files (OLE2 compound documents) and RTF
OLE2_MAGIC = bytes.fromhex("d0cf11e0a1b11ae1")
RTF_MAGIC = b"{\\rtf"
ZIP_MAGIC = b"PK"
SOFFICE_COMMANDS = ("soffice", "libreoffice")
MACOS_SOFFICE = Path("/Applications/LibreOffice.app/Contents/MacOS/soffice")
CONVERT_TIMEOUT_SECONDS = 120
DOCX_FILTER = "docx:MS Word 2007 XML"
OUTPUT_NAME = "source.docx"
FLAT_OPC_NAMESPACE = "http://schemas.microsoft.com/office/2006/xmlPackage"
FLAT_OPC_PROBE_BYTES = 2048
CONTENT_TYPES_PART = "[Content_Types].xml"
DOCX_MAIN_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
# python-docx only accepts the standard document content type; macro-enabled documents and templates have the same body
NONSTANDARD_MAIN_TYPES = (
    "application/vnd.ms-word.document.macroEnabled.main+xml",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.template.main+xml",
    "application/vnd.ms-word.template.macroEnabledTemplate.main+xml",
)


class DocConversionUnavailable(ValueError):
    """No LibreOffice in this environment, so a legacy doc cannot be converted (not a problem of the file)."""


class DocConversionFailed(ValueError):
    """LibreOffice ran but the conversion failed or timed out (damaged file, unsupported format)."""


def is_legacy_doc(data: bytes) -> bool:
    """Legacy Word (OLE2) or RTF: needs LibreOffice."""
    return data.startswith(OLE2_MAGIC) or data.startswith(RTF_MAGIC)


def is_flat_opc(data: bytes) -> bool:
    """Word XML in the single-file form (the whole docx package written as one XML document)."""
    head = data[:FLAT_OPC_PROBE_BYTES]
    return head.lstrip().startswith(b"<?xml") and FLAT_OPC_NAMESPACE.encode() in head


def find_soffice() -> str | None:
    for command in SOFFICE_COMMANDS:
        path = shutil.which(command)
        if path:
            return path
    return str(MACOS_SOFFICE) if MACOS_SOFFICE.is_file() else None


def doc_to_docx(data: bytes) -> bytes:
    """Convert the content of a .doc/.rtf to the content of a docx. Every conversion uses its own user profile
    directory, so concurrent conversions do not fight over the profile lock."""
    soffice = find_soffice()
    if soffice is None:
        raise DocConversionUnavailable("The text is a legacy doc and LibreOffice is not installed here")
    with tempfile.TemporaryDirectory(prefix="doc_convert_") as workdir:
        work = Path(workdir)
        # The extension helps LibreOffice pick the right import filter (the source often names RTF files .doc)
        source, output_dir = work / ("source.rtf" if data.startswith(RTF_MAGIC) else "source.doc"), work / "out"
        source.write_bytes(data)
        command = [
            soffice, f"-env:UserInstallation={(work / 'profile').as_uri()}", "--headless", "--norestore",
            "--convert-to", DOCX_FILTER, "--outdir", str(output_dir), str(source),
        ]
        try:
            subprocess.run(command, check=True, capture_output=True, timeout=CONVERT_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as error:
            raise DocConversionFailed("Converting the legacy doc timed out") from error
        except (subprocess.CalledProcessError, OSError) as error:
            raise DocConversionFailed("Converting the legacy doc failed") from error
        converted = output_dir / OUTPUT_NAME
        if not converted.is_file():
            raise DocConversionFailed("Converting the legacy doc produced no docx")
        return converted.read_bytes()


def flat_opc_to_docx(data: bytes) -> bytes:
    """Rebuild a standard docx (zip) from Word XML in the single-file form: every <pkg:part> becomes a file and
    [Content_Types].xml is generated."""
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        raise DocConversionFailed("The Word XML file declares a DTD; refusing to parse it")
    try:
        parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)
        root = etree.fromstring(data, parser)
        part_tag, name_attr, type_attr = (f"{{{FLAT_OPC_NAMESPACE}}}part", f"{{{FLAT_OPC_NAMESPACE}}}name",
                                          f"{{{FLAT_OPC_NAMESPACE}}}contentType")
        xml_tag, binary_tag = f"{{{FLAT_OPC_NAMESPACE}}}xmlData", f"{{{FLAT_OPC_NAMESPACE}}}binaryData"
        files: dict[str, bytes] = {}
        content_types: dict[str, str] = {}
        for part in root.iter(part_tag):
            name = part.get(name_attr, "").lstrip("/")
            payload = part.find(xml_tag)
            if payload is not None and len(payload):
                files[name] = etree.tostring(payload[0], xml_declaration=True, encoding="UTF-8", standalone=True)
            elif part.find(binary_tag) is not None:
                files[name] = base64.b64decode("".join((part.find(binary_tag).text or "").split()))
            else:
                continue
            content_types[name] = part.get(type_attr, "application/xml")
    except (etree.XMLSyntaxError, ValueError) as error:
        raise DocConversionFailed("The Word XML file cannot be parsed") from error
    if "word/document.xml" not in files:
        raise DocConversionFailed("The Word XML file has no body part")
    ns = "http://schemas.openxmlformats.org/package/2006/content-types"
    types = etree.Element(f"{{{ns}}}Types", nsmap={None: ns})
    etree.SubElement(types, f"{{{ns}}}Default", Extension="rels", ContentType="application/vnd.openxmlformats-package.relationships+xml")
    etree.SubElement(types, f"{{{ns}}}Default", Extension="xml", ContentType="application/xml")
    for name, content_type in content_types.items():
        if not name.endswith(".rels"):
            etree.SubElement(types, f"{{{ns}}}Override", PartName=f"/{name}", ContentType=content_type)
    files[CONTENT_TYPES_PART] = etree.tostring(types, xml_declaration=True, encoding="UTF-8", standalone=True)
    return normalize_docx_content_type(_zip_bytes(files))


def _zip_bytes(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(CONTENT_TYPES_PART, files[CONTENT_TYPES_PART])
        for name, content in files.items():
            if name != CONTENT_TYPES_PART:
                archive.writestr(name, content)
    return buffer.getvalue()


def normalize_docx_content_type(data: bytes) -> bytes:
    """Macro-enabled document or template: set the body content type to the standard docx one, leave the rest as is.
    Returned unchanged when the data is not a zip or needs no change."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            types = archive.read(CONTENT_TYPES_PART).decode("utf-8")
            if not any(bad in types for bad in NONSTANDARD_MAIN_TYPES):
                return data
            patched = types
            for bad in NONSTANDARD_MAIN_TYPES:
                patched = patched.replace(bad, DOCX_MAIN_TYPE)
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as output:
                for item in archive.infolist():
                    output.writestr(item, patched.encode("utf-8") if item.filename == CONTENT_TYPES_PART else archive.read(item.filename))
            return buffer.getvalue()
    except (zipfile.BadZipFile, KeyError, UnicodeDecodeError):
        return data
