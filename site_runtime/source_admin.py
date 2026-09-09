"""Governed approved-source editing and live index activation."""
import csv
import hashlib
import json
import ipaddress
import re
import socket
import sqlite3
import threading
import urllib.request
import zipfile
from io import BytesIO, StringIO
from pathlib import Path, PurePosixPath
from datetime import datetime, timezone
from html.parser import HTMLParser
from time import monotonic
from typing import Literal
from urllib.parse import unquote, urlsplit
from uuid import uuid4
from xml.etree import ElementTree

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pypdf import PdfReader

from .knowledge import (INJECTION_PATTERNS, KnowledgeStore, KnowledgeValidationError,
                        content_checksum, validate_record)
from .ontology_admin import inspect_ontology
from .rag_settings import atomic_json


SOURCE_FIELDS = ("source_id", "title", "canonical_url", "category", "language", "retrieved_at",
                 "checksum", "source_status", "document_version", "content")
MAX_DOWNLOAD_BYTES = 1_000_000
MAX_IMPORT_BYTES = 100_000
MAX_PDF_IMPORT_BYTES = 5_000_000
MAX_PDF_PAGES = 100
MAX_DOCX_IMPORT_BYTES = 5_000_000
MAX_DOCX_ENTRIES = 500
MAX_DOCX_UNCOMPRESSED_BYTES = 20_000_000
MAX_DOCX_PARAGRAPHS = 100
MAX_XLSX_IMPORT_BYTES = 5_000_000
MAX_XLSX_SHEETS = 20
MAX_XLSX_CELLS = 5_000
MAX_CSV_ROWS = 1_000
MAX_CSV_COLUMNS = 50
MAX_PREVIEWS = 100
PREVIEW_TTL_SECONDS = 600
DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
XLSX_MAIN_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
CSV_MEDIA_TYPE = "text/csv"


class UnavailableDocumentScanner:
    """Replaceable boundary for a future scanner; this implementation performs no scan."""
    configured = False

    def scan(self, filename, media_type, body):
        return "not_configured"


def public_record(record):
    return {key: record.get(key) for key in SOURCE_FIELDS}


def revision_of(records):
    raw = json.dumps([public_record(record) for record in records], ensure_ascii=False,
                     sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def import_byte_limit(filename):
    suffix = Path(unquote(filename or "")).suffix.lower()
    return (MAX_PDF_IMPORT_BYTES if suffix == ".pdf" else
            MAX_DOCX_IMPORT_BYTES if suffix == ".docx" else
            MAX_XLSX_IMPORT_BYTES if suffix == ".xlsx" else MAX_IMPORT_BYTES)


def import_limit_message(filename):
    suffix = Path(unquote(filename or "")).suffix.lower()
    return ("Import exceeds the 5-megabyte PDF limit" if suffix == ".pdf" else
            "Import exceeds the 5-megabyte DOCX limit" if suffix == ".docx" else
            "Import exceeds the 5-megabyte XLSX limit" if suffix == ".xlsx" else
            "Import exceeds the 100-kilobyte limit")


def _extract_docx(body):
    try:
        with zipfile.ZipFile(BytesIO(body)) as package:
            entries = package.infolist()
            if not entries or len(entries) > MAX_DOCX_ENTRIES:
                raise KnowledgeValidationError("DOCX archive contains too many package entries")
            total = 0
            for entry in entries:
                parts = Path(entry.filename.replace("\\", "/")).parts
                if (entry.flag_bits & 1 or entry.filename.startswith(("/", "\\")) or
                        "\\" in entry.filename or ".." in parts):
                    raise KnowledgeValidationError("DOCX package contains an unsafe entry")
                total += entry.file_size
                if (total > MAX_DOCX_UNCOMPRESSED_BYTES or
                        entry.file_size > max(100_000, entry.compress_size * 100)):
                    raise KnowledgeValidationError("DOCX package expands beyond the safe extraction limit")
            names = {entry.filename for entry in entries}
            if "[Content_Types].xml" not in names or "word/document.xml" not in names:
                raise KnowledgeValidationError("DOCX package is missing its main document")
            types = package.read("[Content_Types].xml")
            if (b"macroEnabled" in types or b"vbaProject" in types or
                    DOCX_MEDIA_TYPE.encode() not in types or
                    any(name.lower().endswith("vbaproject.bin") for name in names)):
                raise KnowledgeValidationError("Macro-enabled or invalid DOCX files are not supported")
            document = package.read("word/document.xml")
            if b"<!DOCTYPE" in document.upper() or b"<!ENTITY" in document.upper():
                raise KnowledgeValidationError("DOCX document XML contains unsafe declarations")
            root = ElementTree.fromstring(document)
    except KnowledgeValidationError:
        raise
    except (zipfile.BadZipFile, KeyError, ElementTree.ParseError, RuntimeError,
            OSError, ValueError, NotImplementedError) as problem:
        raise KnowledgeValidationError("DOCX is malformed or its text cannot be extracted") from problem
    word = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    paragraphs = []
    for paragraph in root.iter(word + "p"):
        parts = []
        for item in paragraph.iter():
            if item.tag == word + "t" and item.text:
                parts.append(item.text)
            elif item.tag == word + "tab":
                parts.append("\t")
            elif item.tag in {word + "br", word + "cr"}:
                parts.append("\n")
        text = "".join(parts).strip()
        if not text:
            continue
        style = paragraph.find("./" + word + "pPr/" + word + "pStyle")
        style_name = style.get(word + "val", "") if style is not None else ""
        paragraphs.append((text, style_name.lower().startswith("heading")))
        if len(paragraphs) > MAX_DOCX_PARAGRAPHS:
            raise KnowledgeValidationError("DOCX exceeds the 100-paragraph review limit")
        if sum(len(value) for value, _ in paragraphs) > 20_000:
            raise KnowledgeValidationError("Import exceeds the 20,000-character review limit")
    content = "\n\n".join(value for value, _ in paragraphs)
    locations, cursor = [], 0
    for index, (value, heading) in enumerate(paragraphs, 1):
        start = content.find(value, cursor); end = start + len(value); cursor = end
        label = value[:120] if heading else f"Paragraph {index}"
        locations.append({"kind": "section", "label": label, "start": start, "end": end})
    return content, locations, len(paragraphs)


def _xlsx_text(node, spreadsheet):
    return "".join(item.text or "" for item in node.iter(spreadsheet + "t"))


def _extract_xlsx(body):
    try:
        with zipfile.ZipFile(BytesIO(body)) as package:
            entries = package.infolist()
            if not entries or len(entries) > MAX_DOCX_ENTRIES:
                raise KnowledgeValidationError("XLSX archive contains too many package entries")
            total = 0
            for entry in entries:
                parts = PurePosixPath(entry.filename.replace("\\", "/")).parts
                if (entry.flag_bits & 1 or entry.filename.startswith(("/", "\\")) or
                        "\\" in entry.filename or ".." in parts):
                    raise KnowledgeValidationError("XLSX package contains an unsafe entry")
                total += entry.file_size
                if (total > MAX_DOCX_UNCOMPRESSED_BYTES or
                        entry.file_size > max(100_000, entry.compress_size * 100)):
                    raise KnowledgeValidationError("XLSX package expands beyond the safe extraction limit")
            names = {entry.filename for entry in entries}
            required = {"[Content_Types].xml", "xl/workbook.xml", "xl/_rels/workbook.xml.rels"}
            if not required.issubset(names):
                raise KnowledgeValidationError("XLSX package is missing required workbook data")
            types = package.read("[Content_Types].xml")
            if (b"macroEnabled" in types or b"vbaProject" in types or
                    XLSX_MAIN_TYPE.encode() not in types or
                    any("externallink" in name.lower() or name.lower().endswith("vbaproject.bin")
                        for name in names)):
                raise KnowledgeValidationError("Macro-enabled, externally linked, or invalid XLSX files are not supported")
            workbook_raw = package.read("xl/workbook.xml")
            rels_raw = package.read("xl/_rels/workbook.xml.rels")
            if any(marker in value.upper() for value in (types, workbook_raw, rels_raw)
                   for marker in (b"<!DOCTYPE", b"<!ENTITY")):
                raise KnowledgeValidationError("XLSX package XML contains unsafe declarations")
            workbook = ElementTree.fromstring(workbook_raw)
            relationships = ElementTree.fromstring(rels_raw)
            package_ns = "{http://schemas.openxmlformats.org/package/2006/relationships}"
            rel_map = {}
            for relation in relationships.findall(package_ns + "Relationship"):
                if relation.get("TargetMode") == "External":
                    raise KnowledgeValidationError("Externally linked XLSX files are not supported")
                target = PurePosixPath("xl") / PurePosixPath(relation.get("Target", ""))
                normalized = str(target)
                if ".." in target.parts or normalized not in names:
                    raise KnowledgeValidationError("XLSX worksheet relationship is invalid")
                rel_map[relation.get("Id")] = normalized
            spreadsheet = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
            office_rel = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
            sheets = workbook.findall("./" + spreadsheet + "sheets/" + spreadsheet + "sheet")
            if not sheets or len(sheets) > MAX_XLSX_SHEETS:
                raise KnowledgeValidationError("XLSX must contain between 1 and 20 worksheets")
            shared = []
            if "xl/sharedStrings.xml" in names:
                shared_raw = package.read("xl/sharedStrings.xml")
                if b"<!DOCTYPE" in shared_raw.upper() or b"<!ENTITY" in shared_raw.upper():
                    raise KnowledgeValidationError("XLSX shared strings contain unsafe declarations")
                shared_root = ElementTree.fromstring(shared_raw)
                shared = [_xlsx_text(item, spreadsheet) for item in shared_root.findall(spreadsheet + "si")]
                if len(shared) > MAX_XLSX_CELLS:
                    raise KnowledgeValidationError("XLSX shared string table exceeds the safe limit")
            sections, cell_count = [], 0
            for sheet in sheets:
                sheet_name = sheet.get("name", "").strip()
                if (not sheet_name or len(sheet_name) > 31 or any(ord(char) < 32 for char in sheet_name)
                        or "<" in sheet_name or ">" in sheet_name):
                    raise KnowledgeValidationError("XLSX worksheet name is invalid")
                target = rel_map.get(sheet.get(office_rel))
                if not target or not target.startswith("xl/worksheets/"):
                    raise KnowledgeValidationError("XLSX worksheet relationship is invalid")
                sheet_raw = package.read(target)
                if b"<!DOCTYPE" in sheet_raw.upper() or b"<!ENTITY" in sheet_raw.upper():
                    raise KnowledgeValidationError("XLSX worksheet XML contains unsafe declarations")
                root = ElementTree.fromstring(sheet_raw)
                rows, references = [], []
                for cell in root.iter(spreadsheet + "c"):
                    if cell.find(spreadsheet + "f") is not None:
                        raise KnowledgeValidationError("XLSX formulas are not supported; upload values-only data")
                    reference = cell.get("r", "")
                    if not re.fullmatch(r"[A-Z]{1,3}[1-9][0-9]{0,6}", reference):
                        raise KnowledgeValidationError("XLSX cell reference is invalid")
                    kind = cell.get("t", "n")
                    value_node = cell.find(spreadsheet + "v")
                    if kind == "inlineStr":
                        value = _xlsx_text(cell, spreadsheet)
                    elif value_node is None:
                        value = ""
                    elif kind == "s":
                        index = int(value_node.text or "-1")
                        if index < 0 or index >= len(shared):
                            raise KnowledgeValidationError("XLSX shared string reference is invalid")
                        value = shared[index]
                    elif kind in {"n", "str", "b"}:
                        value = value_node.text or ""
                    else:
                        raise KnowledgeValidationError("XLSX cell type is not supported")
                    value = value.replace("\r\n", "\n").replace("\r", "\n").strip()
                    if not value:
                        continue
                    cell_count += 1
                    if cell_count > MAX_XLSX_CELLS:
                        raise KnowledgeValidationError("XLSX exceeds the 5,000-cell review limit")
                    references.append(reference); rows.append(f"{reference}: {value}")
                if rows:
                    sections.append((sheet_name, references[0], references[-1],
                                     f"Worksheet: {sheet_name}\n" + "\n".join(rows)))
            content = "\n\n".join(section[3] for section in sections)
            if len(content) > 20_000:
                raise KnowledgeValidationError("Import exceeds the 20,000-character review limit")
    except KnowledgeValidationError:
        raise
    except (zipfile.BadZipFile, KeyError, ElementTree.ParseError, RuntimeError,
            OSError, ValueError, TypeError, NotImplementedError) as problem:
        raise KnowledgeValidationError("XLSX is malformed or its values cannot be extracted") from problem
    locations, cursor = [], 0
    for name, first, last, section in sections:
        start = content.find(section, cursor); end = start + len(section); cursor = end
        locations.append({"kind": "section", "label": f"{name}!{first}:{last}",
                          "start": start, "end": end})
    return content, locations, len(sheets), cell_count


def _extract_csv(body):
    try:
        decoded = body.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as problem:
        raise KnowledgeValidationError("CSV must be valid UTF-8 text") from problem
    try:
        dialect = csv.Sniffer().sniff(decoded[:8192], delimiters=",;\t")
        rows = list(csv.reader(StringIO(decoded, newline=""), dialect=dialect, strict=True))
    except csv.Error as problem:
        raise KnowledgeValidationError("CSV is malformed or its delimiter is unsupported") from problem
    if not rows or len(rows) > MAX_CSV_ROWS:
        raise KnowledgeValidationError("CSV must contain between 1 and 1,000 rows")
    width = len(rows[0])
    if width < 1 or width > MAX_CSV_COLUMNS or any(len(row) != width for row in rows):
        raise KnowledgeValidationError("CSV rows must each have the same 1–50 columns")
    normalized = []
    for row in rows:
        clean = []
        for cell in row:
            value = cell.replace("\r\n", "\n").replace("\r", "\n").strip()
            if len(value) > 1_000:
                raise KnowledgeValidationError("CSV cell exceeds the 1,000-character limit")
            if value.lstrip().startswith(("=", "+", "-", "@")):
                raise KnowledgeValidationError("CSV formulas are not supported; upload values-only data")
            clean.append(" ".join(value.splitlines()))
        normalized.append(" | ".join(clean))
    sections = []
    for start in range(0, len(normalized), 25):
        end = min(start + 25, len(normalized))
        sections.append((start + 1, end,
                         f"Rows {start + 1}-{end}\n" + "\n".join(normalized[start:end])))
    content = "\n\n".join(section[2] for section in sections)
    if len(content) > 20_000:
        raise KnowledgeValidationError("Import exceeds the 20,000-character review limit")
    locations, cursor = [], 0
    for first, last, section in sections:
        start = content.find(section, cursor); end = start + len(section); cursor = end
        locations.append({"kind": "section", "label": f"Rows {first}-{last}",
                          "start": start, "end": end})
    delimiter = {",": "comma", ";": "semicolon", "\t": "tab"}[dialect.delimiter]
    return content, locations, len(rows), width, delimiter


class SourceRecordInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)
    source_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    title: str = Field(min_length=1, max_length=200)
    canonical_url: str = Field(min_length=1, max_length=500)
    category: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    language: Literal["sv", "en"]
    retrieved_at: str = Field(min_length=1, max_length=40)
    document_version: str = Field(default="", max_length=100)
    content: str = Field(min_length=1, max_length=20000)

    @field_validator("title", "canonical_url", "retrieved_at", "document_version", "content")
    @classmethod
    def safe_text(cls, value):
        if any(ord(character) < 32 and character not in "\n\r\t" for character in value):
            raise ValueError("Control characters are not allowed")
        if INJECTION_PATTERNS.search(value) or "<" in value or ">" in value:
            raise ValueError("Markup or instruction-like source text is not allowed")
        return value

    @field_validator("retrieved_at")
    @classmethod
    def timestamp(cls, value):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as problem:
            raise ValueError("Use an ISO-8601 retrieval timestamp") from problem
        if parsed.tzinfo is None:
            raise ValueError("Retrieval timestamp must include a timezone")
        return value

    def stored(self, status="active"):
        record = self.model_dump()
        record.update(source_status=status, checksum=content_checksum(record["content"].strip()))
        return public_record(validate_record(record))


class SourceChange(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    revision: str = Field(min_length=64, max_length=64, pattern=r"^[a-f0-9]{64}$")
    operation: Literal["add", "update", "archive", "activate"]
    source_id: str | None = Field(default=None, min_length=1, max_length=100,
                                  pattern=r"^[A-Za-z0-9_-]+$")
    record: SourceRecordInput | None = None
    confirmed: bool
    import_token: str | None = Field(default=None, min_length=32, max_length=32,
                                     pattern=r"^[a-f0-9]{32}$")

    @model_validator(mode="after")
    def shape(self):
        if self.confirmed is not True:
            raise ValueError("Explicit source review confirmation is required")
        if self.operation == "add" and (self.source_id is not None or self.record is None):
            raise ValueError("Add requires a record and no existing source ID")
        if self.operation == "update" and (self.source_id is None or self.record is None):
            raise ValueError("Update requires an existing source ID and record")
        if self.operation in {"archive", "activate"} and (self.source_id is None or self.record is not None):
            raise ValueError("Status changes require only an existing source ID")
        if self.import_token is not None and self.record is None:
            raise ValueError("An import token requires an approved source record")
        return self


class DocumentExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    characters: int = Field(ge=20, le=20000)
    pages: int | None = Field(default=None, ge=1, le=MAX_PDF_PAGES)
    locations: list[dict] = Field(default_factory=list, max_length=100)

    @field_validator("locations")
    @classmethod
    def safe_locations(cls, locations):
        for location in locations:
            if (not isinstance(location, dict) or set(location) != {"kind", "label", "start", "end"}
                    or location["kind"] not in {"page", "section"}
                    or not isinstance(location["label"], str) or not location["label"]
                    or len(location["label"]) > 120 or "<" in location["label"] or ">" in location["label"]
                    or any(ord(character) < 32 for character in location["label"])
                    or not isinstance(location["start"], int) or not isinstance(location["end"], int)
                    or location["start"] < 0 or location["end"] <= location["start"]):
                raise ValueError("Document extraction locations are invalid")
        return locations

    @model_validator(mode="after")
    def bounded_locations(self):
        if any(location["end"] > self.characters for location in self.locations):
            raise ValueError("Document extraction location exceeds extracted content")
        if self.pages is not None and sum(item["kind"] == "page" for item in self.locations) > self.pages:
            raise ValueError("Document extraction page count is invalid")
        return self


class ApprovedDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)
    document_id: str = Field(min_length=32, max_length=32, pattern=r"^[a-f0-9]{32}$")
    display_filename: str = Field(min_length=1, max_length=200)
    source_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    canonical_url: str = Field(min_length=1, max_length=500)
    media_type: Literal["text/plain", "text/markdown", "text/x-markdown", "application/pdf",
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        "text/csv"]
    byte_size: int = Field(ge=20, le=MAX_PDF_IMPORT_BYTES)
    checksum: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    version: str = Field(max_length=100)
    language: Literal["sv", "en"]
    category: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    approved_at: str = Field(min_length=20, max_length=40)
    status: Literal["active", "archived"]
    extraction: DocumentExtraction

    @field_validator("display_filename", "version")
    @classmethod
    def safe_manifest_text(cls, value):
        if any(ord(character) < 32 for character in value) or "<" in value or ">" in value:
            raise ValueError("Document metadata contains unsafe characters")
        return value

    @field_validator("approved_at")
    @classmethod
    def approval_timestamp(cls, value):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("Approval time must include a timezone")
        return value


class ApprovedDocumentManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: Literal[1] = 1
    documents: list[ApprovedDocument] = Field(default_factory=list, max_length=200)


class DocumentArchiveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    manifest_revision: str = Field(min_length=64, max_length=64, pattern=r"^[a-f0-9]{64}$")
    source_revision: str = Field(min_length=64, max_length=64, pattern=r"^[a-f0-9]{64}$")
    confirmed: Literal[True]


class DocumentReindexRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    manifest_revision: str = Field(min_length=64, max_length=64, pattern=r"^[a-f0-9]{64}$")
    confirmed: Literal[True]


class SourceConflict(ValueError):
    pass


class SourceFetchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    revision: str = Field(min_length=64, max_length=64, pattern=r"^[a-f0-9]{64}$")
    source_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")


class SourceRefreshApproval(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    preview_token: str = Field(min_length=32, max_length=32, pattern=r"^[a-f0-9]{32}$")
    confirmed: bool

    @model_validator(mode="after")
    def confirmation(self):
        if self.confirmed is not True:
            raise ValueError("Explicit refresh approval is required")
        return self


class _ReadableText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.primary = 0
        self.primary_text = []
        self.fallback_text = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript", "svg", "nav", "footer", "header"}:
            self.skip += 1
        if tag in {"main", "article"}:
            self.primary += 1

    def handle_endtag(self, tag):
        if tag in {"main", "article"} and self.primary:
            self.primary -= 1
        if tag in {"script", "style", "noscript", "svg", "nav", "footer", "header"} and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if self.skip:
            return
        cleaned = " ".join(data.split())
        if cleaned:
            self.fallback_text.append(cleaned)
            if self.primary:
                self.primary_text.append(cleaned)

    def text(self):
        return "\n".join(self.primary_text or self.fallback_text)


def _validate_fetch_url(url):
    from website_assistant.site_settings import web_url
    try:
        web_url(url)
    except ValueError as error:
        raise KnowledgeValidationError(str(error)) from error


def _require_public_host(hostname):
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)}
    except OSError as problem:
        raise KnowledgeValidationError("Approved source host could not be resolved") from problem
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise KnowledgeValidationError("Approved source host did not resolve exclusively to public addresses")


class _SafeRedirects(urllib.request.HTTPRedirectHandler):
    max_redirections = 3

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_fetch_url(newurl)
        _require_public_host(urlsplit(newurl).hostname)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class SourceAdmin:
    def __init__(self, rag, document_scanner=None):
        self.rag = rag
        self.tools = rag.tools
        self.lock = rag.lock
        self.path = self.tools.knowledge.source_path
        self.backup_path = self.path.parent / "approved_sources.previous.json"
        self.preview_lock = threading.Lock()
        self.previews = {}
        self.imports = {}
        self.document_directory = self.path.parent / "approved_documents"
        self.document_manifest_path = self.path.parent / "approved_documents.json"
        self.document_scanner = document_scanner or UnavailableDocumentScanner()

    def status(self):
        store = self.tools.knowledge
        try:
            document_count = len(self._document_manifest().documents)
        except (OSError, ValueError, TypeError):
            document_count = None
        return {"revision": revision_of(store.source_records),
                "records": [public_record(record) for record in store.source_records],
                "active_records": len(store.records), "index_status": store.index_status,
                "recovery_snapshot": self.backup_path.name, "automatic_crawling": False,
                "approved_documents": document_count}

    def preview_import(self, filename, content_type, body):
        filename = unquote(filename)
        if (not isinstance(filename, str) or not filename or len(filename) > 200 or
                Path(filename).name != filename or "\\" in filename):
            raise KnowledgeValidationError("Import filename is missing or invalid")
        suffix = Path(filename).suffix.lower()
        if suffix not in {".txt", ".md", ".pdf", ".docx", ".xlsx", ".csv"}:
            raise KnowledgeValidationError("Import supports only .txt, .md, .pdf, .docx, .xlsx, and .csv files")
        media_type = (content_type or "").split(";", 1)[0].strip().lower()
        allowed_types = ({"application/pdf"} if suffix == ".pdf" else
                         {DOCX_MEDIA_TYPE} if suffix == ".docx" else
                         {XLSX_MEDIA_TYPE} if suffix == ".xlsx" else
                         {CSV_MEDIA_TYPE} if suffix == ".csv" else
                         {"text/plain", "text/markdown", "text/x-markdown"})
        if media_type not in allowed_types:
            raise KnowledgeValidationError("Import content type does not match the file extension")
        if not isinstance(body, bytes) or len(body) > import_byte_limit(filename):
            raise KnowledgeValidationError(import_limit_message(filename))
        scan_outcome = self.document_scanner.scan(filename, media_type, body)
        if scan_outcome not in {"clean", "not_configured"}:
            raise KnowledgeValidationError("Document security scan did not approve the import")
        pages, page_texts, docx_locations, paragraphs = None, None, None, None
        xlsx_locations, sheets, cells = None, None, None
        csv_locations, rows, columns, delimiter = None, None, None, None
        if suffix == ".pdf":
            if not body.startswith(b"%PDF-"):
                raise KnowledgeValidationError("PDF signature is missing or invalid")
            try:
                reader = PdfReader(BytesIO(body), strict=True)
                if reader.is_encrypted:
                    raise KnowledgeValidationError("Encrypted PDFs are not supported")
                pages = len(reader.pages)
                if pages < 1 or pages > MAX_PDF_PAGES:
                    raise KnowledgeValidationError("PDF must contain between 1 and 100 pages")
                extracted = []
                for page in reader.pages:
                    extracted.append(page.extract_text() or "")
                    if sum(len(part) for part in extracted) > 20_000:
                        raise KnowledgeValidationError("Import exceeds the 20,000-character review limit")
                page_texts = [part.replace("\r\n", "\n").replace("\r", "\n").strip()
                              for part in extracted]
                content = "\n\n".join(page_texts)
            except KnowledgeValidationError:
                raise
            except Exception as problem:
                raise KnowledgeValidationError("PDF is malformed or its text cannot be extracted") from problem
        elif suffix == ".docx":
            if not body.startswith(b"PK"):
                raise KnowledgeValidationError("DOCX ZIP signature is missing or invalid")
            content, docx_locations, paragraphs = _extract_docx(body)
        elif suffix == ".xlsx":
            if not body.startswith(b"PK"):
                raise KnowledgeValidationError("XLSX ZIP signature is missing or invalid")
            content, xlsx_locations, sheets, cells = _extract_xlsx(body)
        elif suffix == ".csv":
            content, csv_locations, rows, columns, delimiter = _extract_csv(body)
        else:
            try:
                content = body.decode("utf-8-sig", errors="strict")
            except UnicodeDecodeError as problem:
                raise KnowledgeValidationError("Import must be valid UTF-8 text") from problem
        content = content.replace("\r\n", "\n").replace("\r", "\n").strip()
        if len(content) < 20:
            if suffix == ".pdf":
                raise KnowledgeValidationError("PDF contains no extractable text; scanned or image-only PDFs require OCR")
            if suffix == ".docx":
                raise KnowledgeValidationError("DOCX contains no extractable paragraph text")
            if suffix == ".xlsx":
                raise KnowledgeValidationError("XLSX contains no extractable cell values")
            if suffix == ".csv":
                raise KnowledgeValidationError("CSV contains no extractable cell values")
            raise KnowledgeValidationError("Import must contain at least 20 readable characters")
        if len(content) > 20000:
            raise KnowledgeValidationError("Import exceeds the 20,000-character review limit")
        # Reuse the same content boundary as an eventual confirmed source save.
        SourceRecordInput(source_id="import-preview", title="Import preview",
            canonical_url="https://example.com/", category="import",
            language="sv", retrieved_at=datetime.now(timezone.utc).isoformat(), content=content)
        title = " ".join(Path(filename).stem.replace("_", " ").replace("-", " ").split())
        if not title or len(title) > 200 or INJECTION_PATTERNS.search(title) or "<" in title or ">" in title:
            title = "Imported source"
        locations = []
        if csv_locations is not None:
            locations = csv_locations
        elif xlsx_locations is not None:
            locations = xlsx_locations
        elif docx_locations is not None:
            locations = docx_locations
        elif page_texts is not None:
            cursor = 0
            for index, page in enumerate(page_texts, 1):
                if page:
                    start = content.find(page, cursor); end = start + len(page)
                    locations.append({"kind": "page", "label": f"Page {index}",
                                      "start": start, "end": end}); cursor = end
        else:
            matches = list(re.finditer(r"(?m)^#{1,6}\s+.+$", content)) if suffix == ".md" else []
            if matches:
                for index, match in enumerate(matches):
                    end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
                    locations.append({"kind": "section", "label": match.group().lstrip("# ")[:120],
                                      "start": match.start(), "end": end})
            else:
                locations = [{"kind": "section", "label": "Section 1",
                              "start": 0, "end": len(content)}]
        return {"filename": filename, "file_type": suffix[1:], "suggested_title": title,
                "content": content, "characters": len(content),
                "checksum": content_checksum(content), "activated": False, "pages": pages,
                "paragraphs": paragraphs, "locations": locations,
                "sheets": sheets, "cells": cells, "rows": rows, "columns": columns,
                "delimiter": delimiter, "malware_scan": scan_outcome}

    def stage_import(self, filename, content_type, body, owner):
        preview = self.preview_import(filename, content_type, body)
        token, now = uuid4().hex, monotonic()
        media_type = (content_type or "").split(";", 1)[0].strip().lower()
        with self.preview_lock:
            self.imports = {key: value for key, value in self.imports.items()
                            if value["expires_at"] > now}
            while len(self.imports) >= MAX_PREVIEWS:
                self.imports.pop(next(iter(self.imports)))
            self.imports[token] = {"owner": hashlib.sha256(owner.encode()).hexdigest(),
                "expires_at": now + PREVIEW_TTL_SECONDS, "filename": preview["filename"],
                "media_type": media_type, "body": body, "preview": preview}
        return {**preview, "import_token": token, "expires_in_seconds": PREVIEW_TTL_SECONDS}

    def _approved_import(self, token, owner, record):
        if token is None:
            return None
        now, owner_hash = monotonic(), hashlib.sha256(owner.encode()).hexdigest()
        with self.preview_lock:
            staged = self.imports.get(token)
        if (staged is None or staged["expires_at"] <= now or staged["owner"] != owner_hash or
                content_checksum(record.content.strip()) != staged["preview"]["checksum"]):
            raise SourceConflict("Import preview is missing, expired, changed, or belongs to another session. Load the file again.")
        return staged

    def _document_manifest(self):
        if not self.document_manifest_path.exists():
            return ApprovedDocumentManifest()
        payload = json.loads(self.document_manifest_path.read_text(encoding="utf-8"))
        try:
            return ApprovedDocumentManifest.model_validate(payload)
        except (ValueError, TypeError) as problem:
            raise KnowledgeValidationError("Approved document manifest is invalid") from problem

    @staticmethod
    def _manifest_revision(manifest):
        raw = json.dumps(manifest.model_dump(), ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def documents_status(self):
        with self.lock:
            manifest = self._document_manifest()
            return {"revision": self._manifest_revision(manifest),
                    "source_revision": revision_of(self.tools.knowledge.source_records),
                    "documents": [document.model_dump() for document in manifest.documents],
                    "index_status": self.tools.knowledge.index_status}

    def _document(self, document_id):
        manifest = self._document_manifest()
        document = next((item for item in manifest.documents
                         if item.document_id == document_id), None)
        if document is None:
            raise SourceConflict("Approved document not found")
        suffix = {"text/plain": ".txt", "text/markdown": ".md",
                  "text/x-markdown": ".md", "application/pdf": ".pdf",
                  DOCX_MEDIA_TYPE: ".docx", XLSX_MEDIA_TYPE: ".xlsx",
                  CSV_MEDIA_TYPE: ".csv"}[document.media_type]
        path = self.document_directory / f"{document.document_id}{suffix}"
        if (not path.is_file() or "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
                != document.checksum):
            raise KnowledgeValidationError("Approved document file is missing or does not match its manifest")
        return manifest, document, path

    def download(self, document_id):
        with self.lock:
            _, document, path = self._document(document_id)
            return path, document.display_filename, document.media_type

    def archive_document(self, document_id, request: DocumentArchiveRequest, owner=""):
        with self.lock:
            manifest, document, _ = self._document(document_id)
            if request.manifest_revision != self._manifest_revision(manifest):
                raise SourceConflict("Documents changed since this page loaded. Refresh and try again.")
            if document.status == "archived":
                raise SourceConflict("Approved document is already archived")
            candidate = manifest.model_copy(deep=True)
            next(item for item in candidate.documents
                 if item.document_id == document_id).status = "archived"
            atomic_json(self.document_manifest_path, candidate.model_dump())
            try:
                self.change(SourceChange(revision=request.source_revision, operation="archive",
                    source_id=document.source_id, record=None, confirmed=True), owner)
            except Exception:
                atomic_json(self.document_manifest_path, manifest.model_dump())
                raise
            return self.documents_status()

    def reindex_document(self, document_id, request: DocumentReindexRequest):
        with self.lock:
            manifest, document, _ = self._document(document_id)
            if request.manifest_revision != self._manifest_revision(manifest):
                raise SourceConflict("Documents changed since this page loaded. Refresh and try again.")
            if document.status != "active":
                raise SourceConflict("Only active approved documents can be re-indexed")
            self.rag.rebuild()
            return self.documents_status()

    @staticmethod
    def _write_document(path, body):
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_bytes(body)
            try:
                temporary.chmod(0o600)
            except OSError:
                pass
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def _rollback_document(self, manifest, path):
        try:
            atomic_json(self.document_manifest_path, manifest.model_dump())
        except OSError:
            pass
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    def _read_disk(self):
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        raw = payload.get("records") if isinstance(payload, dict) else payload
        if not isinstance(raw, list):
            raise KnowledgeValidationError("Approved source snapshot must contain records")
        return payload, [validate_record(record) for record in raw]

    def _current_record(self, revision, source_id):
        disk_payload, disk_records = self._read_disk()
        current_revision = revision_of(self.tools.knowledge.source_records)
        if revision != current_revision or revision_of(disk_records) != current_revision:
            raise SourceConflict("Sources changed since this page loaded. Refresh before fetching.")
        record = next((public_record(item) for item in disk_records
                       if item["source_id"] == source_id), None)
        if record is None:
            raise SourceConflict("The selected source no longer exists")
        return disk_payload, record

    def _download(self, url):
        from website_assistant.crawl_transport import PublicTransport
        from urllib.parse import urljoin, urlsplit
        initial = urlsplit(url)
        for _ in range(4):
            _validate_fetch_url(url)
            result = PublicTransport().fetch(url, max_bytes=MAX_DOWNLOAD_BYTES, timeout=15)
            if result.status in {301,302,303,307,308}:
                target = urljoin(url,result.headers.get("location",""))
                if urlsplit(target).netloc != initial.netloc:
                    raise KnowledgeValidationError("Refresh redirect leaves the source website")
                url=target
                continue
            if result.status != 200:
                raise KnowledgeValidationError("Source could not be fetched")
            content_type = result.headers.get("content-type", "")
            if "text/html" not in content_type:
                raise KnowledgeValidationError("Source must be HTML")
            text = result.body.decode("utf-8")
            parser = _ReadableText()
            parser.feed(text)
            return parser.text(), url
        raise KnowledgeValidationError("Source redirect limit exceeded")

    def preview_refresh(self, request: SourceFetchRequest, owner):
        with self.lock:
            _, record = self._current_record(request.revision, request.source_id)
        fetched_content, final_url = self._download(record["canonical_url"])
        refreshed = {key: record.get(key, "") for key in (
            "source_id", "title", "canonical_url", "category", "language", "document_version")}
        refreshed.update(retrieved_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                         content=fetched_content)
        validated = SourceRecordInput(**refreshed)
        with self.lock:
            self._current_record(request.revision, request.source_id)
        token = uuid4().hex
        now = monotonic()
        with self.preview_lock:
            self.previews = {key: value for key, value in self.previews.items()
                             if value["expires_at"] > now}
            while len(self.previews) >= MAX_PREVIEWS:
                self.previews.pop(next(iter(self.previews)))
            self.previews[token] = {"owner": hashlib.sha256(owner.encode()).hexdigest(),
                "expires_at": now + PREVIEW_TTL_SECONDS, "revision": request.revision,
                "source_id": request.source_id, "record": validated}
        return {"preview_token": token, "expires_in_seconds": PREVIEW_TTL_SECONDS,
                "source_id": request.source_id, "canonical_url": final_url,
                "previous_content": record["content"], "fetched_content": fetched_content,
                "previous_checksum": record["checksum"],
                "fetched_checksum": content_checksum(fetched_content),
                "changed": record["content"].strip() != fetched_content,
                "previous_characters": len(record["content"]),
                "fetched_characters": len(fetched_content)}

    def apply_refresh(self, approval: SourceRefreshApproval, owner):
        now = monotonic()
        owner_hash = hashlib.sha256(owner.encode()).hexdigest()
        with self.preview_lock:
            preview = self.previews.get(approval.preview_token)
            if preview is None:
                raise SourceConflict("Refresh preview is missing, expired, or belongs to another session. Fetch again.")
            if preview["expires_at"] <= now:
                self.previews.pop(approval.preview_token, None)
                raise SourceConflict("Refresh preview is missing, expired, or belongs to another session. Fetch again.")
            if preview["owner"] != owner_hash:
                raise SourceConflict("Refresh preview is missing, expired, or belongs to another session. Fetch again.")
        result = self.change(SourceChange(revision=preview["revision"], operation="update",
                                          source_id=preview["source_id"], record=preview["record"],
                                          confirmed=True))
        with self.preview_lock:
            self.previews.pop(approval.preview_token, None)
        result["changed_source_id"] = preview["source_id"]
        return result

    def change(self, change: SourceChange, owner=""):
        with self.lock:
            previous_store = self.tools.knowledge
            disk_payload, disk_records = self._read_disk()
            current_revision = revision_of(previous_store.source_records)
            if change.revision != current_revision or revision_of(disk_records) != current_revision:
                raise SourceConflict("Sources changed since this page loaded. Refresh before editing.")
            records = [public_record(record) for record in disk_records]
            positions = {record["source_id"]: index for index, record in enumerate(records)}
            if change.operation == "add":
                if change.record.source_id in positions:
                    raise KnowledgeValidationError("Source ID already exists")
                records.append(change.record.stored())
            else:
                if change.source_id not in positions:
                    raise SourceConflict("The selected source no longer exists")
                index = positions[change.source_id]
                if change.operation == "update":
                    if change.record.source_id != change.source_id:
                        raise KnowledgeValidationError("Source ID cannot be changed; add a new source instead")
                    records[index] = change.record.stored(records[index]["source_status"])
                else:
                    records[index]["source_status"] = "archived" if change.operation == "archive" else "active"
            if len(records) > 200:
                raise KnowledgeValidationError("The demo supports at most 200 sources")
            checked = [validate_record(record) for record in records]
            if len({record["source_id"] for record in checked}) != len(checked):
                raise KnowledgeValidationError("Duplicate source IDs")
            if len({(record["canonical_url"], record["language"]) for record in checked}) != len(checked):
                raise KnowledgeValidationError("Only one source per canonical URL and language is allowed")
            active = [record for record in checked if record["source_status"] == "active"]
            if not active:
                raise KnowledgeValidationError("At least one active source is required")
            active_urls = {record["canonical_url"] for record in active}
            if any(contact.get("active") and contact["official_source_url"] not in active_urls
                   for contact in self.tools.contacts):
                raise KnowledgeValidationError("An active contact would lose its approved source")
            candidate_knowledge = type("CandidateKnowledge", (), {"records": active,
                "approved_urls": active_urls})()
            report = inspect_ontology(type("CandidateTools", (), {"ontology": self.tools.ontology,
                "knowledge": candidate_knowledge, "contacts": self.tools.contacts})())
            if report["issues"] or any(row["issues"] for row in report["relationships"] + report["aliases"]):
                raise KnowledgeValidationError("The change would invalidate an ontology source reference")

            staged = self._approved_import(change.import_token, owner, change.record) if change.record else None
            manifest_before, document_path = None, None
            if staged:
                manifest_before = self._document_manifest()
                file_checksum = "sha256:" + hashlib.sha256(staged["body"]).hexdigest()
                if any(item.checksum == file_checksum for item in manifest_before.documents):
                    raise KnowledgeValidationError("This document content is already approved")
                document_id = uuid4().hex
                suffix = Path(staged["filename"]).suffix.lower()
                document_path = self.document_directory / f"{document_id}{suffix}"
                document = {"document_id": document_id, "display_filename": staged["filename"],
                    "source_id": change.record.source_id, "canonical_url": change.record.canonical_url,
                    "media_type": staged["media_type"], "byte_size": len(staged["body"]),
                    "checksum": file_checksum, "version": change.record.document_version,
                    "language": change.record.language, "category": change.record.category,
                    "approved_at": datetime.now(timezone.utc).isoformat(), "status": "active",
                    "extraction": {"characters": staged["preview"]["characters"],
                                   "pages": staged["preview"]["pages"],
                                   "locations": staged["preview"]["locations"]}}
                manifest_candidate = ApprovedDocumentManifest(
                    documents=[*manifest_before.documents, ApprovedDocument(**document)])

            candidate_payload = dict(disk_payload) if isinstance(disk_payload, dict) else {}
            candidate_payload["snapshot"] = datetime.now(timezone.utc).strftime("admin-%Y-%m-%dT%H:%M:%SZ")
            candidate_payload["records"] = records
            try:
                if staged:
                    self.document_directory.mkdir(parents=True, exist_ok=True)
                    self._write_document(document_path, staged["body"])
                    atomic_json(self.document_manifest_path, manifest_candidate.model_dump())
                atomic_json(self.backup_path, disk_payload)
                atomic_json(self.path, candidate_payload)
            except Exception:
                if staged:
                    self._rollback_document(manifest_before, document_path)
                raise
            try:
                candidate = KnowledgeStore(self.path, use_index=False)
                candidate.settings = previous_store.settings
                candidate.settings_warning = previous_store.settings_warning
                candidate.embedding_client = previous_store.embedding_client
                candidate.embedding_cache = previous_store.embedding_cache
                generated_at = datetime.now(timezone.utc).isoformat()
                atomic_json(candidate.index_path, {"generated_at": generated_at,
                                                   "records": candidate.records,
                                                   "chunks": candidate.chunks})
                candidate.index_status, candidate.index_generated_at = "ready", generated_at
                if candidate.embedding_client.configured:
                    try:
                        candidate.embedding_cache.prune(candidate.embedding_client.model,
                                                        candidate.chunks)
                    except (OSError, sqlite3.Error):
                        pass
            except Exception:
                # Best-effort rollback; the recovery snapshot remains available if storage is unavailable.
                atomic_json(self.path, disk_payload)
                if staged:
                    self._rollback_document(manifest_before, document_path)
                raise
            self.tools.knowledge = candidate
            if staged:
                with self.preview_lock:
                    self.imports.pop(change.import_token, None)
        return self.status()
