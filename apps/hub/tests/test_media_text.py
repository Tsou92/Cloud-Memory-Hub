import base64
import io
from uuid import uuid4

import pytest
from cloud_memory_hub.media_text import (
    MediaTextError,
    extract_document_text,
    rewrite_record_media,
)
from cloud_memory_hub.source_capture import SourceConfiguration
from pypdf import PdfReader, PdfWriter


def _base_pdf(text="Hello PDF"):
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
    ]
    stream = f"BT /F1 24 Tf 72 720 Td ({text}) Tj ET".encode()
    objs.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, obj in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets[1:]:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    ).encode()
    return bytes(out)


def _document(name, media_type, data, *, source_type="base64"):
    if source_type == "base64":
        data = base64.b64encode(data).decode()
    return {
        "type": "document",
        "name": name,
        "source": {"type": source_type, "media_type": media_type, "data": data},
    }


def test_text_document_is_extracted_without_retaining_bytes():
    raw = b"hello from a text attachment"
    derived = extract_document_text(_document("notes.txt", "text/plain", raw))
    assert "hello from a text attachment" in derived
    assert "notes.txt" in derived
    assert "text/plain" in derived
    assert base64.b64encode(raw).decode() not in derived


def test_pdf_text_layer_is_extracted():
    derived = extract_document_text(_document("report.pdf", "application/pdf", _base_pdf()))
    assert "Hello PDF" in derived
    assert "1 页" in derived
    assert "pypdf" in derived


def test_text_source_type_is_supported():
    block = _document("notes.md", "text/markdown", "# title", source_type="text")
    assert "# title" in extract_document_text(block)


def test_unsupported_media_type_is_rejected():
    with pytest.raises(MediaTextError) as error:
        extract_document_text(_document("a.docx", "application/vnd.openxmlformats", b"PK"))
    assert error.value.code == "media_type_unsupported"


def test_scanned_pdf_without_text_is_rejected():
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    stream = io.BytesIO()
    writer.write(stream)
    with pytest.raises(MediaTextError) as error:
        extract_document_text(_document("scan.pdf", "application/pdf", stream.getvalue()))
    assert error.value.code == "media_pdf_no_text"


def test_encrypted_pdf_is_rejected():
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.encrypt("secret")
    stream = io.BytesIO()
    writer.write(stream)
    with pytest.raises(MediaTextError) as error:
        extract_document_text(_document("secret.pdf", "application/pdf", stream.getvalue()))
    assert error.value.code == "media_pdf_encrypted"


def test_too_many_pdf_pages_is_rejected(monkeypatch):
    monkeypatch.setattr("cloud_memory_hub.media_text.MAX_PDF_PAGES", 1)
    reader = PdfReader(io.BytesIO(_base_pdf()))
    writer = PdfWriter()
    writer.append(reader)
    writer.add_blank_page(width=612, height=792)
    stream = io.BytesIO()
    writer.write(stream)
    with pytest.raises(MediaTextError) as error:
        extract_document_text(_document("long.pdf", "application/pdf", stream.getvalue()))
    assert error.value.code == "media_pdf_too_many_pages"


def test_extracted_text_over_limit_is_rejected(monkeypatch):
    monkeypatch.setattr("cloud_memory_hub.media_text.MAX_EXTRACTED_CHARS", 10)
    with pytest.raises(MediaTextError) as error:
        extract_document_text(_document("notes.txt", "text/plain", b"x" * 11))
    assert error.value.code == "media_text_too_large"


def test_rewrite_record_replaces_claude_document_blocks():
    record = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {"type": "text", "text": "please review"},
                _document("notes.txt", "text/plain", b"hello"),
            ],
        },
    }
    assert rewrite_record_media(record, adapter_id="claude-code") == 1
    block = record["message"]["content"][1]
    assert block["type"] == "text"
    assert "hello" in block["text"]
    assert base64.b64encode(b"hello").decode() not in block["text"]


def test_rewrite_record_replaces_codex_document_blocks():
    record = {
        "type": "response_item",
        "payload": {
            "type": "message",
            "content": [_document("notes.txt", "text/plain", b"hello")],
        },
    }
    assert rewrite_record_media(record, adapter_id="codex") == 1
    assert record["payload"]["content"][0]["type"] == "input_text"


def test_image_blocks_are_left_for_the_guard():
    record = {
        "type": "user",
        "message": {"content": [{"type": "image", "source": {"data": "..."}}]},
    }
    assert rewrite_record_media(record, adapter_id="claude-code") == 0
    assert record["message"]["content"][0]["type"] == "image"


def test_extraction_flag_does_not_change_the_source_binding():
    values = {
        "adapter_id": "claude-code",
        "source_id": str(uuid4()),
        "scope_id": str(uuid4()),
        "source_root": "/tmp/source",
        "workspace": "/tmp/workspace",
    }
    assert (
        SourceConfiguration(**values, media_text_extraction=False).binding_digest()
        == SourceConfiguration(**values, media_text_extraction=True).binding_digest()
    )


def test_extraction_flag_is_strict():
    values = {
        "adapter_id": "claude-code",
        "source_id": str(uuid4()),
        "scope_id": str(uuid4()),
        "source_root": "/tmp/source",
        "workspace": "/tmp/workspace",
    }
    with pytest.raises(ValueError):
        SourceConfiguration(**values, media_text_extraction="yes")
