"""Versioned read messages shared by the NAS and local connector."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from cloud_memory_hub.events import canonical_uuid
from cloud_memory_hub.privacy import POLICY_VERSION

TEXT_BLOCK = 4096
READ_RESPONSE_BYTES = 120 * 1024


class ScopeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    scope_id: str

    @field_validator("scope_id")
    @classmethod
    def valid_scope(cls, value):
        return value if value == "global" else canonical_uuid(value)


class ListRequest(ScopeRequest):
    limit: int = Field(default=10, strict=True, ge=1, le=10)
    offset: int = Field(default=0, strict=True, ge=0, le=2**31 - 1)


class SearchRequest(ScopeRequest):
    query: str = Field(strict=True, min_length=1, max_length=4096, repr=False)
    limit: int = Field(default=5, strict=True, ge=1, le=10)
    privacy_policy: Literal[POLICY_VERSION] = POLICY_VERSION

    @field_validator("query")
    @classmethod
    def nonempty(cls, value):
        if not value.strip():
            raise ValueError("empty_query")
        return value


class HistorySearchRequest(ScopeRequest):
    """Bounded keyword search over this scope's accepted original text.

    Deliberately not a subclass of ListRequest: the read path dispatches on
    ListRequest first, and a history search must go through query filtering.
    """

    query: str = Field(strict=True, min_length=1, max_length=4096, repr=False)
    limit: int = Field(default=10, strict=True, ge=1, le=10)
    offset: int = Field(default=0, strict=True, ge=0, le=2**31 - 1)
    privacy_policy: Literal[POLICY_VERSION] = POLICY_VERSION

    @field_validator("query")
    @classmethod
    def nonempty(cls, value):
        if not value.strip():
            raise ValueError("empty_query")
        return value


class GetRequest(ScopeRequest):
    record_id: str = Field(strict=True, pattern=r"^[A-Za-z0-9_:-]{1,200}$")
    content_offset: int = Field(default=0, strict=True, ge=0, le=2**31 - 1)


SessionReference = Annotated[str, Field(strict=True, pattern=r"^cmhe:[0-9a-f-]{36}$")]
SessionCursor = Annotated[str, Field(strict=True, pattern=r"^[0-9]{1,19}:[0-9]{1,7}$")]


class SessionBriefRequest(ScopeRequest):
    limit: int = Field(default=3, strict=True, ge=1, le=3)
    query: str | None = Field(default=None, strict=True, max_length=4096, repr=False)
    exclude_conversation_id: str | None = None

    @field_validator("exclude_conversation_id")
    @classmethod
    def valid_conversation(cls, value):
        return canonical_uuid(value) if value is not None else None


class SessionMarkdownRequest(ScopeRequest):
    session_ref: SessionReference
    cursor: SessionCursor | None = None


SESSION_MARKDOWN_PATH = "/hub/v1/sessions/markdown"
SESSION_MARKDOWN_CHARS = 16384
ReadOperation = Literal[
    "memory-list",
    "memory-search",
    "memory-get",
    "history-list",
    "history-search",
    "history-get",
    "session-briefs",
]
READ_OPERATIONS = {
    "memory-list": ("/hub/v1/memories/list", ListRequest),
    "memory-search": ("/hub/v1/memories/search", SearchRequest),
    "memory-get": ("/hub/v1/memories/get", GetRequest),
    "history-list": ("/hub/v1/history/list", ListRequest),
    "history-search": ("/hub/v1/history/search", HistorySearchRequest),
    "history-get": ("/hub/v1/history/get", GetRequest),
    "session-briefs": ("/hub/v1/sessions/recent", SessionBriefRequest),
}


def is_history_read(operation):
    return operation.startswith(("history-", "session-"))


class ReadItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    record_id: str = Field(min_length=1, max_length=200)
    kind: Literal["memory", "history"]
    content: str = Field(max_length=TEXT_BLOCK, repr=False)
    content_offset: int = Field(ge=0)
    next_content_offset: int | None = Field(default=None, ge=0)
    agent_id: str = Field(max_length=512)
    session_id: str | None = Field(default=None, max_length=512)
    source: str | None = Field(default=None, max_length=512)
    layer: str | None = Field(default=None, max_length=64)
    source_raw_ids: tuple[str, ...] = Field(default=(), max_length=32)
    references_truncated: bool = False
    memory_at: int | None = None

    @field_validator("source_raw_ids")
    @classmethod
    def bounded_references(cls, value):
        if any(not item or len(item) > 200 for item in value):
            raise ValueError("invalid_reference")
        return value


class ReadReply(ScopeRequest):
    protocol_version: Literal[1] = 1
    instance_id: str
    device_id: str
    device_revision: int = Field(strict=True, ge=1)
    operation: ReadOperation
    content_trust: Literal["untrusted_history"] = "untrusted_history"
    items: tuple[ReadItem, ...] = Field(max_length=10)
    next_offset: int | None = Field(default=None, ge=0)
    candidate_limit_reached: bool = False

    @field_validator("instance_id", "device_id")
    @classmethod
    def valid_uuid(cls, value):
        return canonical_uuid(value)

    @field_validator("protocol_version", mode="before")
    @classmethod
    def strict_version(cls, value):
        if type(value) is not int or value != 1:
            raise ValueError("invalid_read_version")
        return value


class SessionMarkdownReply(ScopeRequest):
    protocol_version: Literal[1] = 1
    instance_id: str
    device_id: str
    device_revision: int = Field(strict=True, ge=1)
    operation: Literal["session-markdown"] = "session-markdown"
    content_trust: Literal["untrusted_history"] = "untrusted_history"
    session_ref: SessionReference
    markdown: str = Field(strict=True, max_length=SESSION_MARKDOWN_CHARS, repr=False)
    next_cursor: SessionCursor | None = None

    @field_validator("instance_id", "device_id")
    @classmethod
    def valid_uuid(cls, value):
        return canonical_uuid(value)

    @field_validator("protocol_version", mode="before")
    @classmethod
    def strict_version(cls, value):
        return ReadReply.strict_version(value)
