"""Safe, in-memory text extraction for files attached to FRIDAY chat."""

from __future__ import annotations

import re
from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile


MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_EXTRACTED_CHARS = 8_000
MAX_DOCX_XML_BYTES = 5 * 1024 * 1024

TEXT_EXTENSIONS = {
    ".cfg",
    ".conf",
    ".css",
    ".csv",
    ".env",
    ".go",
    ".h",
    ".hpp",
    ".htm",
    ".html",
    ".ini",
    ".java",
    ".js",
    ".json",
    ".jsx",
    ".log",
    ".md",
    ".markdown",
    ".php",
    ".properties",
    ".py",
    ".rb",
    ".rs",
    ".scss",
    ".sh",
    ".sql",
    ".svg",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}


class FileReadingError(ValueError):
    """Raised when a file cannot be safely converted to model-readable text."""


def extract_file_text(filename: str, data: bytes, media_type: str | None = None) -> tuple[str, bool]:
    """Return normalized text and whether it was shortened to the context limit."""

    safe_name = Path(filename).name.strip()
    if not safe_name or safe_name in {".", ".."}:
        raise FileReadingError("filename is required")
    if not data:
        raise FileReadingError("file is empty")
    if len(data) > MAX_FILE_BYTES:
        raise FileReadingError("file exceeds the 10 MB limit")

    extension = Path(safe_name).suffix.lower()
    normalized_media_type = (media_type or "").split(";", 1)[0].strip().lower()
    if extension == ".pdf" or normalized_media_type == "application/pdf":
        text = _extract_pdf(data)
    elif extension == ".docx" or normalized_media_type == (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    ):
        text = _extract_docx(data)
    elif extension in TEXT_EXTENSIONS or normalized_media_type.startswith("text/"):
        text = _decode_text(data)
    else:
        raise FileReadingError("unsupported file type; use a text, code, CSV, JSON, PDF, or DOCX file")

    text = _normalize_text(text)
    if not text:
        raise FileReadingError("no readable text was found in the file")
    truncated = len(text) > MAX_EXTRACTED_CHARS
    if truncated:
        text = text[:MAX_EXTRACTED_CHARS].rstrip() + "\n\n[File content truncated by FRIDAY]"
    return text, truncated


def _decode_text(data: bytes) -> str:
    if b"\x00" in data[:4096] and not data.startswith((b"\xff\xfe", b"\xfe\xff")):
        raise FileReadingError("the selected file appears to be binary")
    for encoding in ("utf-8-sig", "utf-16", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise FileReadingError("the file text encoding is not supported")


def _extract_pdf(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - dependency is installed in the app image
        raise FileReadingError("PDF reading support is not installed") from exc

    try:
        reader = PdfReader(BytesIO(data))
        parts: list[str] = []
        character_count = 0
        for page in reader.pages[:100]:
            page_text = page.extract_text() or ""
            parts.append(page_text)
            character_count += len(page_text)
            if character_count > MAX_EXTRACTED_CHARS:
                break
        return "\n\n".join(parts)
    except Exception as exc:
        raise FileReadingError("the PDF could not be read or is encrypted") from exc


def _extract_docx(data: bytes) -> str:
    try:
        with ZipFile(BytesIO(data)) as archive:
            try:
                document_info = archive.getinfo("word/document.xml")
            except KeyError as exc:
                raise FileReadingError("the DOCX document body is missing") from exc
            if document_info.file_size > MAX_DOCX_XML_BYTES:
                raise FileReadingError("the DOCX document is too complex to read safely")
            root = ElementTree.fromstring(archive.read(document_info))
    except (BadZipFile, ElementTree.ParseError) as exc:
        raise FileReadingError("the DOCX file is invalid or corrupted") from exc

    paragraphs: list[str] = []
    for paragraph in root.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"):
        fragments: list[str] = []
        for node in paragraph.iter():
            if node.tag.endswith("}t") and node.text:
                fragments.append(node.text)
            elif node.tag.endswith("}tab"):
                fragments.append("\t")
            elif node.tag.endswith("}br"):
                fragments.append("\n")
        value = "".join(fragments).strip()
        if value:
            paragraphs.append(value)
        if sum(map(len, paragraphs)) > MAX_EXTRACTED_CHARS:
            break
    return "\n\n".join(paragraphs)


def _normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return text.strip()
