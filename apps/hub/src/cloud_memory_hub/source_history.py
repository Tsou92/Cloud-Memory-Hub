"""History view of the complete, filtered events retained at acceptance."""

from one_memory.hy_memory_bridge import HySdkRawRecord, _joined_thread

from cloud_memory_hub.events import canonical_uuid
from cloud_memory_hub.original_archive import validate_original
from cloud_memory_hub.remote_access import AccessError

PREFIX = "cmhe:"


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
