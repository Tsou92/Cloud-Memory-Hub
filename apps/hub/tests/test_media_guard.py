"""The media guard must reject real media blocks, not any dict that happens to
carry a 'type' or 'attachments' key. Tool results routinely embed JSON Schema
fragments whose 'type' is a list, and schema properties can be named
'attachments'."""

import pytest
from cloud_memory_hub.capture_store import CaptureError
from cloud_memory_hub.source_capture import _reject_unsupported_media


@pytest.mark.parametrize(
    "record",
    [
        {"type": "image", "source": {"data": "..."}},
        {"content": [{"type": "document", "name": "a.pdf"}]},
        {"message": {"attachments": [{"kind": "file"}]}},
    ],
)
def test_real_media_is_rejected(record):
    with pytest.raises(CaptureError) as error:
        _reject_unsupported_media(record)
    assert str(error.value) == "source_media_not_supported"


@pytest.mark.parametrize(
    "record",
    [
        {"type": "tool_result", "content": "plain text"},
        {"toolUseResult": {"type": "text", "file": {"filePath": "/tmp/x.py"}}},
        {"content": [{"type": "text", "text": "hello"}]},
        # JSON Schema fragments appear inside real tool results.
        {"input_schema": {"type": ["object", "null"], "properties": {}}},
        {"json_schema": {"type": "array", "items": {"type": "string"}}},
        # A schema property literally named 'attachments' is not a file.
        {"parameters": {"properties": {"attachments": {"type": "array"}}}},
        {"metadata": {"type": "turn", "attachments": []}},
    ],
)
def test_non_media_records_pass(record):
    """A tool result is not a media block, and must never abort a whole batch."""
    _reject_unsupported_media(record)
