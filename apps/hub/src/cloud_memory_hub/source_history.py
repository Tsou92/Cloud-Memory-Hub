"""History view of the complete, filtered events retained at acceptance."""

import re
import unicodedata

from one_memory.hy_memory_bridge import HySdkRawRecord, _joined_thread
from pydantic import ValidationError

from cloud_memory_hub.events import canonical_uuid
from cloud_memory_hub.original_archive import validate_original
from cloud_memory_hub.privacy import PrivacyError
from cloud_memory_hub.read_contract import SESSION_MARKDOWN_CHARS
from cloud_memory_hub.remote_access import AccessError
from cloud_memory_hub.remote_ingest import Conversation, Operation

PREFIX = "cmhe:"
_RECENT_ROWS = 48
_BRIEF_EVENTS = 12
_EXCERPT_CHARS = 600
_SESSION_SCAN_ROWS = 128
_MARKDOWN_TITLE = "# 会话原文\n\n"
_CURSOR = re.compile(r"([1-9][0-9]{0,18}):(0|[1-9][0-9]{0,6})")
_ROLE_HEADING = {"user": "用户", "assistant": "助手", "tool": "工具", "operation": "操作"}
_CURRENT_REVISION = """NOT EXISTS (
    SELECT 1 FROM cmh_event_receipts AS newer
    WHERE newer.scope_id=receipt.scope_id AND newer.source_id=receipt.source_id
      AND newer.conversation_id=receipt.conversation_id AND newer.record_id=receipt.record_id
      AND newer.revision > receipt.revision
)"""


def _parse_cursor(cursor):
    if cursor is None:
        return 0, 0
    match = _CURSOR.fullmatch(cursor) if isinstance(cursor, str) else None
    if match is None:
        raise AccessError("invalid_request", 422)
    position, offset = map(int, match.groups())
    if position >= 2**63:
        raise AccessError("invalid_request", 422)
    return position, offset


def _session_key(row):
    return row["source_id"], row["conversation_id"]


def _normalize(text):
    return unicodedata.normalize("NFKC", text).casefold()


def _relevance(summary, query, terms):
    normalized = _normalize(summary)
    return bool(query and query in normalized), sum(term in normalized for term in terms)


class SourceHistory:
    def __init__(self, ingest):
        self.ingest = ingest

    def _record(self, row):
        event = validate_original(row, self.ingest.access.instance_id)
        canonical_uuid(row["receipt_id"])
        return HySdkRawRecord(
            raw_id=PREFIX + row["receipt_id"],
            content=event.canonical_payload(),
            user_id=f"cmh:{event.instance_id}:{event.scope_id}",
            agent_id="cmh:" + event.source_id,
            session_id=event.conversation_id,
            source=row["adapter_id"],
            memory_at=int(event.occurred_at.timestamp()) if event.occurred_at else None,
        )

    def _page(self, namespace, limit, offset):
        prefix = f"cmh:{self.ingest.access.instance_id}:"
        if not namespace.startswith(prefix):
            raise AccessError("original_scope_invalid", 503)
        scope = namespace[len(prefix) :]
        with self.ingest._database() as db:
            total = db.execute(
                "SELECT count(*) FROM cmh_event_receipts WHERE scope_id=?", (scope,)
            ).fetchone()[0]
            rows = db.execute(
                "SELECT receipt.*,source.adapter_id FROM cmh_event_receipts receipt "
                "JOIN cmh_sources source USING(source_id) WHERE receipt.scope_id=? "
                "ORDER BY receipt.rowid DESC LIMIT ? OFFSET ?",
                (scope, limit, offset),
            ).fetchall()
            return [self._record(row) for row in rows], total

    def _get(self, identity):
        try:
            if not identity.startswith(PREFIX):
                return None
            receipt = canonical_uuid(identity[len(PREFIX) :])
        except ValueError:
            return None
        with self.ingest._database() as db:
            row = db.execute(
                "SELECT receipt.*,source.adapter_id FROM cmh_event_receipts receipt "
                "JOIN cmh_sources source USING(source_id) WHERE receipt.receipt_id=?",
                (receipt,),
            ).fetchone()
            return self._record(row) if row is not None else None

    async def page(self, namespace, limit, offset):
        return await _joined_thread(self._page, namespace, limit, offset)

    async def get(self, identity):
        return await _joined_thread(self._get, identity)

    def _current(self, db, scope_id, position, hidden):
        row = db.execute(
            f"SELECT receipt.rowid AS position, receipt.* FROM cmh_event_receipts AS receipt "
            f"WHERE receipt.scope_id=? AND receipt.rowid=? AND {_CURRENT_REVISION}",
            (scope_id, position),
        ).fetchone()
        if row is None or PREFIX + row["receipt_id"] in hidden:
            return None
        # A purge can physically remove the highest revision. The signed source
        # rule still covers its stable identity, even if an older row remains.
        if "purged" in hidden.values():
            event = validate_original(row, self.ingest.access.instance_id)
            try:
                self.ingest.require_source_allowed(event)
            except AccessError as error:
                if error.code in {"governance_source_suppressed", "governance_content_suppressed"}:
                    return None
                raise
        return row

    def _messages(self, row, privacy):
        event = validate_original(row, self.ingest.access.instance_id)
        canonical_uuid(row["receipt_id"])
        try:
            content = privacy.protect(event.content).value
            if event.kind == "conversation_turn":
                return [
                    (message.role, message.text)
                    for message in Conversation.model_validate(content).messages
                ]
            return [("operation", Operation.model_validate(content).text)]
        except (PrivacyError, ValidationError):
            raise AccessError("read_unavailable", 503) from None

    def _confirm_session_rows(self, scope_id, rows):
        # A new revision or forget can land after the first read transaction.
        hidden = self.ingest.suppressed(scope_id)
        with self.ingest._database() as db:
            for position, expected in rows.items():
                if self._current(db, scope_id, position, hidden) != expected:
                    raise AccessError("session_changed", 409)

    def _session_briefs(self, scope_id, *, limit, query, exclude_conversation_id, privacy):
        if type(limit) is not int or not 1 <= limit <= 3:
            raise AccessError("invalid_request", 422)
        query = unicodedata.normalize("NFKC", query or "").strip()
        try:
            if privacy.protect(query).value != query:
                raise AccessError("query_requires_refiltering", 422)
        except PrivacyError:
            raise AccessError("query_requires_refiltering", 422) from None
        query = query.casefold()
        terms = tuple(dict.fromkeys(re.findall(r"\w+", query)))[:24]
        hidden = self.ingest.suppressed(scope_id)
        sessions = {}
        with self.ingest._database() as db:
            candidates = db.execute(
                "SELECT rowid AS position, receipt_id, source_id, conversation_id "
                "FROM cmh_event_receipts INDEXED BY cmh_original_scope "
                "WHERE scope_id=? ORDER BY rowid DESC LIMIT ?",
                (scope_id, _RECENT_ROWS),
            )
            loaded = 0
            for candidate in candidates:
                if (
                    candidate["conversation_id"] == exclude_conversation_id
                    or PREFIX + candidate["receipt_id"] in hidden
                ):
                    continue
                key = _session_key(candidate)
                if key in sessions and len(sessions[key][1]) == 2:
                    continue
                row = self._current(db, scope_id, candidate["position"], hidden)
                if row is None:
                    continue
                messages = self._messages(row, privacy)
                loaded += 1
                _, excerpts = sessions.setdefault(key, (row, {}))
                for role, content in reversed(messages):
                    if role in {"user", "assistant"} and role not in excerpts:
                        compact = " ".join(content.split())
                        if compact:
                            text = compact[:_EXCERPT_CHARS]
                            excerpts[role] = (text + ("…" if text != compact else ""), row)
                if loaded >= _BRIEF_EVENTS:
                    break
        ranked = []
        for anchor, excerpts in sessions.values():
            if not excerpts:
                continue
            summary = "\n".join(
                f"{label}：{excerpts[role][0]}"
                for role, label in (("user", "最近请求"), ("assistant", "最近进展"))
                if role in excerpts
            )
            summary = privacy.protect(summary).value
            ranked.append((_relevance(summary, query, terms), summary, anchor, excerpts))
        ranked.sort(key=lambda item: (item[0], item[2]["position"]), reverse=True)
        selected = ranked[:limit]
        evidence = {}
        for _, _, anchor, excerpts in selected:
            evidence[anchor["position"]] = anchor
            evidence.update((row["position"], row) for _, row in excerpts.values())
        self._confirm_session_rows(scope_id, evidence)
        return [
            {"session_ref": PREFIX + anchor["receipt_id"], "summary": summary}
            for _, summary, anchor, _ in selected
        ]

    def _session_markdown(self, scope_id, *, session_ref, cursor, privacy):
        try:
            if not isinstance(session_ref, str) or not session_ref.startswith(PREFIX):
                raise ValueError
            receipt_id = canonical_uuid(session_ref[len(PREFIX) :])
        except ValueError:
            raise AccessError("invalid_request", 422) from None
        position, offset = _parse_cursor(cursor)
        title = _MARKDOWN_TITLE if cursor is None else ""
        # The character limit also bounds UTF-8 bytes, including the title, so
        # JSON escaping stays within the existing remote response budget.
        budget = SESSION_MARKDOWN_CHARS - len(title.encode())
        hidden = self.ingest.suppressed(scope_id)
        with self.ingest._database() as db:
            identity = db.execute(
                "SELECT rowid FROM cmh_event_receipts WHERE scope_id=? AND receipt_id=?",
                (scope_id, receipt_id),
            ).fetchone()
            anchor = None if identity is None else self._current(db, scope_id, identity[0], hidden)
            if anchor is None:
                raise AccessError("record_not_found", 404)
            validate_original(anchor, self.ingest.access.instance_id)
            if position > anchor["position"]:
                raise AccessError("invalid_request", 422)
            evidence = {anchor["position"]: anchor}
            row, scanned = self._markdown_row(db, scope_id, anchor, position, offset, hidden)
            if row is None:
                markdown = ""
                next_cursor = f"{scanned}:0" if scanned < anchor["position"] else None
            else:
                markdown = "".join(
                    f"## {_ROLE_HEADING[role]}\n\n{text}\n\n"
                    for role, text in self._messages(row, privacy)
                )
                if offset >= len(markdown):
                    raise AccessError("session_changed", 409)
                markdown = markdown[offset:]
                encoded = markdown.encode()
                if len(encoded) > budget:
                    markdown = encoded[:budget].decode(errors="ignore")
                    next_cursor = f"{row['position']}:{offset + len(markdown)}"
                else:
                    next_cursor = (
                        f"{row['position']}:0" if row["position"] < anchor["position"] else None
                    )
                evidence[row["position"]] = row
        self._confirm_session_rows(scope_id, evidence)
        return {
            "session_ref": session_ref,
            "markdown": title + markdown,
            "next_cursor": next_cursor,
        }

    def _markdown_row(self, db, scope_id, anchor, position, offset, hidden):
        if offset:
            row = self._current(db, scope_id, position, hidden)
            if row is None or _session_key(row) != _session_key(anchor):
                raise AccessError("session_changed", 409)
            return row, position
        candidates = db.execute(
            "SELECT rowid AS position, receipt_id, source_id, conversation_id "
            "FROM cmh_event_receipts INDEXED BY cmh_original_scope "
            "WHERE scope_id=? AND rowid>? AND rowid<=? ORDER BY rowid LIMIT ?",
            (scope_id, position, anchor["position"], _SESSION_SCAN_ROWS),
        )
        for candidate in candidates:
            position = candidate["position"]
            if (
                _session_key(candidate) != _session_key(anchor)
                or PREFIX + candidate["receipt_id"] in hidden
            ):
                continue
            row = self._current(db, scope_id, position, hidden)
            if row is not None:
                return row, position
        return None, position

    async def session_briefs(self, scope_id, *, limit, query, exclude_conversation_id, privacy):
        return await _joined_thread(
            self._session_briefs,
            scope_id,
            limit=limit,
            query=query,
            exclude_conversation_id=exclude_conversation_id,
            privacy=privacy,
        )

    async def session_markdown(self, scope_id, *, session_ref, cursor, privacy):
        return await _joined_thread(
            self._session_markdown,
            scope_id,
            session_ref=session_ref,
            cursor=cursor,
            privacy=privacy,
        )
