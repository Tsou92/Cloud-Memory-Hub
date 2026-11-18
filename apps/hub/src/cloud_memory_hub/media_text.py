"""Local text extraction for supported attachments.

Phase 1 deliberately supports only text-bearing documents: the PDF text layer
and text files. Images, scanned PDFs and binary office documents stay rejected,
and the original bytes never leave the source device.
"""

import base64
import binascii
import hashlib
import io

MAX_MEDIA_BYTES = 20 * 1024 * 1024
MAX_PDF_PAGES = 50
MAX_EXTRACTED_CHARS = 64 * 1024
MAX_FILENAME_CHARS = 200

_TEXT_MEDIA_TYPES = frozenset({"text/plain", "text/markdown", "text/x-markdown"})
_TEXT_SUFFIXES = (".txt", ".md", ".markdown")
_DOCUMENT_BLOCK_TYPES = frozenset({"document"})


class MediaTextError(Exception):
    """Only fixed reason codes; never retain the rejected content."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _filename(block, source):
    for value in (
        block.get("title"),
        block.get("name"),
        source.get("filename"),
        source.get("name"),
    ):
        if isinstance(value, str) and value.strip():
            name = value.strip().replace("\r", " ").replace("\n", " ")
            name = "".join(ch for ch in name if ord(ch) >= 32 and ord(ch) != 127)
            return name[:MAX_FILENAME_CHARS] or "attachment"
    return "attachment"


def _decode_base64(value):
    if not isinstance(value, str):
        raise MediaTextError("media_source_invalid")
    if len(value) > (MAX_MEDIA_BYTES * 4 // 3) + 8:
        raise MediaTextError("media_too_large")
    if value.startswith("data:"):
        _, _, value = value.partition(",")
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise MediaTextError("media_source_invalid") from None
    if len(raw) > MAX_MEDIA_BYTES:
        raise MediaTextError("media_too_large")
    return raw


def _text_media_type(media_type, name):
    if isinstance(media_type, str):
        normalized = media_type.split(";", 1)[0].strip().lower()
        return normalized if normalized in _TEXT_MEDIA_TYPES else None
    if isinstance(name, str) and name.lower().endswith(_TEXT_SUFFIXES):
        return "text/plain"
    return None


def _pdf_text(raw):
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(raw), strict=False)
        if reader.is_encrypted:
            raise MediaTextError("media_pdf_encrypted")
        pages = len(reader.pages)
        if pages > MAX_PDF_PAGES:
            raise MediaTextError("media_pdf_too_many_pages")
        chunks = [page.extract_text() or "" for page in reader.pages]
    except MediaTextError:
        raise
    except Exception:
        raise MediaTextError("media_extract_failed") from None
    text = "\n\n".join(chunk.strip() for chunk in chunks if chunk.strip()).strip()
    if not text:
        raise MediaTextError("media_pdf_no_text")
    return text, pages


def _document_text(block):
    source = block.get("source")
    if not isinstance(source, dict):
        raise MediaTextError("media_source_invalid")
    source_type = source.get("type")
    if source_type not in {"base64", "text"}:
        # URL/file references are not fetched in phase 1.
        raise MediaTextError("media_source_unsupported")
    media_type = source.get("media_type") or block.get("media_type")
    name = _filename(block, source)
    if source_type == "text":
        value = source.get("data")
        if not isinstance(value, str):
            raise MediaTextError("media_source_invalid")
        raw = value.encode("utf-8")
        normalized = _text_media_type(media_type, name)
        if normalized is None:
            raise MediaTextError("media_type_unsupported")
        text, pages, extractor = value, 1, "text"
        media_type = normalized
    else:
        raw = _decode_base64(source.get("data"))
        media_type = (
            media_type.split(";", 1)[0].strip().lower() if isinstance(media_type, str) else None
        )
        if media_type == "application/pdf":
            text, pages, extractor = (*_pdf_text(raw), "pypdf")
        else:
            normalized = _text_media_type(media_type, name)
            if normalized is None:
                raise MediaTextError("media_type_unsupported")
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                raise MediaTextError("media_text_decode_failed") from None
            pages, extractor = 1, "text"
            media_type = normalized
    if len(text) > MAX_EXTRACTED_CHARS:
        raise MediaTextError("media_text_too_large")
    digest = hashlib.sha256(raw).hexdigest()
    header = (
        f"[附件 {name} | {media_type} | {pages} 页 | "
        f"{len(raw)} bytes | sha256:{digest[:16]} | {extractor}]"
    )
    return header + "\n" + text.strip()


def extract_document_text(block):
    """Return derived text for a supported document block, or None."""
    if not isinstance(block, dict) or block.get("type") not in _DOCUMENT_BLOCK_TYPES:
        return None
    return _document_text(block)


def rewrite_record_media(record, *, adapter_id):
    """Replace supported document blocks with derived text blocks in place.

    Unknown media is left untouched so the existing guard still fails closed.
    Returns the number of replaced blocks.
    """
    if not isinstance(record, dict):
        return 0
    lists = []
    message = record.get("message")
    if (
        adapter_id == "claude-code"
        and isinstance(message, dict)
        and isinstance(message.get("content"), list)
    ):
        lists.append(message["content"])
    payload = record.get("payload")
    if (
        adapter_id == "codex"
        and isinstance(payload, dict)
        and isinstance(payload.get("content"), list)
    ):
        lists.append(payload["content"])
    text_type = "text" if adapter_id == "claude-code" else "input_text"
    replaced = 0
    for blocks in lists:
        for index, block in enumerate(blocks):
            text = extract_document_text(block)
            if text is None:
                continue
            blocks[index] = {"type": text_type, "text": text}
            replaced += 1
    return replaced
