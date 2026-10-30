"""Index proven SDK outputs to accepted originals, without another writer queue."""

import hashlib
import json
import logging
from dataclasses import replace
from importlib.metadata import version

from one_memory.hy_memory_bridge import _joined_thread
from one_memory.memory_processing import MemoryTaskRunner
from one_memory.memory_server.durable_queue import COMPLETED

from cloud_memory_hub.chain_inventory import ROUND_LAYER, chain_id
from cloud_memory_hub.chat_evidence import capture_chat, check_chat, commit_chat, initialize_chat
from cloud_memory_hub.chat_graph import ChatGraph
from cloud_memory_hub.governance_journal import GovernanceError
from cloud_memory_hub.governance_plan import RelationNode
from cloud_memory_hub.input_context import (
    capture_context,
    check_context,
    commit_context,
    context_parents,
    initialize_context,
    stored_context,
)
from cloud_memory_hub.original_archive import validate_original
from cloud_memory_hub.productivity_inventory import trace_id
from cloud_memory_hub.reconcile_evidence import (
    capture_inputs,
    check_inputs,
    commit_inputs,
    initialize_inputs,
)
from cloud_memory_hub.reconcile_graph import ReconcileGraph
from cloud_memory_hub.remote_access import AccessError
from cloud_memory_hub.remote_ingest import write_request
from cloud_memory_hub.sdk_reconcile import MAX_REFERENCES, proven_write
from cloud_memory_hub.source_history import PREFIX

_LOG = logging.getLogger(__name__)
_IGNORED_REQUEST_FIELDS = {"event_at", "time_quality", "priority"}


def _digest(links):
    return hashlib.sha256(json.dumps(sorted(links), separators=(",", ":")).encode()).hexdigest()


class OutputLineage:
    def __init__(self, ingest, bridge, governance):
        self.ingest, self.bridge, self.governance = ingest, bridge, governance
        self._cursor, self._running = 0, False
        with ingest._database(writing=True) as db:
            found = db.execute(
                "SELECT name FROM sqlite_master WHERE name='cmh_lineage_schema'"
            ).fetchone()
            if found is None:
                if db.execute(
                    "SELECT name FROM sqlite_master WHERE name IN "
                    "('cmh_event_outcomes','cmh_event_output_links')"
                ).fetchone():
                    raise AccessError("lineage_store_unrecognized", 503)
                db.execute(
                    "CREATE TABLE cmh_lineage_schema(id INTEGER PRIMARY KEY,version INTEGER)"
                )
                db.execute("INSERT INTO cmh_lineage_schema VALUES (1,1)")
                db.execute(
                    "CREATE TABLE cmh_event_outcomes (receipt_id TEXT PRIMARY KEY "
                    "REFERENCES cmh_event_receipts(receipt_id), links_digest TEXT NOT NULL)"
                )
                db.execute(
                    "CREATE TABLE cmh_event_output_links (receipt_id TEXT NOT NULL "
                    "REFERENCES cmh_event_outcomes(receipt_id), kind TEXT NOT NULL "
                    "CHECK(kind IN ('raw','memory')), output_id TEXT NOT NULL, "
                    "PRIMARY KEY(receipt_id,kind,output_id))"
                )
                db.execute(
                    "CREATE INDEX cmh_output_origins ON cmh_event_output_links(kind,output_id)"
                )
            initialize_context(db)
            initialize_inputs(db)
            initialize_chat(db)
            self._check(db)

    @staticmethod
    def _check(db):
        version = db.execute("SELECT version FROM cmh_lineage_schema WHERE id=1").fetchone()
        if version is None or version[0] != 1:
            raise AccessError("lineage_store_version_unsupported", 503)
        db.execute("SELECT receipt_id,links_digest FROM cmh_event_outcomes LIMIT 0")
        db.execute("SELECT receipt_id,kind,output_id FROM cmh_event_output_links LIMIT 0")
        check_context(db)
        check_inputs(db)
        check_chat(db)

    def _request(self, job_id, frozen):
        self.governance.require_ready()
        with self.ingest._database() as db:
            self._check(db)
            row = db.execute(
                "SELECT * FROM cmh_event_receipts WHERE receipt_id=?", (job_id,)
            ).fetchone()
            if row is None:
                raise ValueError("lineage_original_missing")
            event = validate_original(row, self.ingest.access.instance_id)
            rebuilt = write_request(event)
            if frozen is not None and frozen.model_dump(
                exclude=_IGNORED_REQUEST_FIELDS
            ) != rebuilt.model_dump(exclude=_IGNORED_REQUEST_FIELDS):
                raise ValueError("lineage_input_mismatch")
            return event, rebuilt

    @staticmethod
    def _stored_links(db, receipt):
        links = tuple(
            (row[0], row[1])
            for row in db.execute(
                "SELECT kind,output_id FROM cmh_event_output_links "
                "WHERE receipt_id=? ORDER BY kind,output_id LIMIT ?",
                (receipt, MAX_REFERENCES + 1),
            )
        )
        if len(links) > MAX_REFERENCES:
            raise ValueError("lineage_links_invalid")
        return links

    def _commit(self, job_id, event, links, context, inputs, chat):
        self.governance.require_ready()
        with self.ingest._database(writing=True) as db:
            self._check(db)
            row = db.execute(
                "SELECT * FROM cmh_event_receipts WHERE receipt_id=?", (job_id,)
            ).fetchone()
            if row is None or validate_original(row, event.instance_id) != event:
                raise ValueError("lineage_original_changed")
            previous = db.execute(
                "SELECT links_digest FROM cmh_event_outcomes WHERE receipt_id=?", (job_id,)
            ).fetchone()
            if previous is not None:
                if previous[0] != _digest(links) or self._stored_links(db, job_id) != links:
                    raise ValueError("lineage_result_changed")
            else:
                db.execute("INSERT INTO cmh_event_outcomes VALUES (?,?)", (job_id, _digest(links)))
                db.executemany(
                    "INSERT INTO cmh_event_output_links VALUES (?,?,?)",
                    [(job_id, kind, identity) for kind, identity in links],
                )
            commit_context(db, job_id, context, event)
            rebuilt = write_request(event)
            request = MemoryTaskRunner.request_for(rebuilt.idempotency_key, rebuilt.input_records)
            commit_inputs(db, job_id, inputs, request)
            commit_chat(db, job_id, chat, request)

    async def observe(self, job_id, frozen):
        # Core can also hold non-Hub jobs. Only our own instance's events have archives.
        if frozen is not None and not frozen.idempotency_key.startswith(
            f"cmh:{self.ingest.access.instance_id}:"
        ):
            return
        if version("hy-memory") != "2.0.5.dev3":
            raise ValueError("lineage_sdk_version_unsupported")
        bridge = self.bridge()
        async with bridge.exclusive_access():
            event, rebuilt = await _joined_thread(self._request, job_id, frozen)
            request = MemoryTaskRunner.request_for(rebuilt.idempotency_key, rebuilt.input_records)
            _, links, result = await proven_write(bridge, job_id, request, "add")
            sdk = await bridge._get_client()
            context = await _joined_thread(
                capture_context, result, sdk._config.coding.db_path, request
            )
            inputs = capture_inputs(result, request)
            chat = capture_chat(result, request)
            await _joined_thread(self._commit, job_id, event, links, context, inputs, chat)

    def _pending(self):
        self.governance.require_ready()
        with self.ingest._database() as db:
            self._check(db)
            return db.execute(
                "SELECT rowid,receipt_id,request_id FROM cmh_event_receipts receipt "
                "WHERE rowid>? AND (NOT EXISTS (SELECT 1 FROM cmh_event_contexts context "
                "WHERE context.receipt_id=receipt.receipt_id) OR NOT EXISTS "
                "(SELECT 1 FROM cmh_event_inputs inputs "
                "WHERE inputs.receipt_id=receipt.receipt_id) OR NOT EXISTS "
                "(SELECT 1 FROM cmh_event_chat_inputs chat "
                "WHERE chat.receipt_id=receipt.receipt_id)) "
                "ORDER BY rowid LIMIT 32",
                (self._cursor,),
            ).fetchall()

    async def reconcile_completed(self):
        if self._running:
            return 0
        self._running, count = True, 0
        try:
            rows = await _joined_thread(self._pending)
            if not rows:
                self._cursor = 0
            for row in rows:
                self._cursor = row[0]
                try:
                    job = await _joined_thread(self.ingest._queue().get, row[2])
                    if job is not None and job.job_id == row[1] and job.state == COMPLETED:
                        await self.observe(job.job_id, None)
                        count += 1
                except Exception:
                    _LOG.debug("lineage_reconcile_deferred")
            return count
        except Exception:
            _LOG.warning("lineage_reconcile_unavailable")
            return count
        finally:
            self._running = False

    def _references(self, record):
        instance_prefix = f"cmh:{self.ingest.access.instance_id}:"
        if not record.user_id.startswith(instance_prefix):
            return ()
        scope = record.user_id[len(instance_prefix) :]
        targets = [("memory", record.memory_id)]
        targets.extend(("raw", raw) for raw in record.source_raw_ids[:32])
        originals, outputs = set(), []
        with self.ingest._database() as db:
            self._check(db)
            for kind, identity in targets:
                rows = db.execute(
                    "SELECT receipt.*,outcome.links_digest FROM cmh_event_output_links link "
                    "JOIN cmh_event_receipts receipt USING(receipt_id) "
                    "JOIN cmh_event_outcomes outcome USING(receipt_id) "
                    "WHERE link.kind=? AND link.output_id=? AND receipt.scope_id=? "
                    "ORDER BY receipt.rowid LIMIT 33",
                    (kind, identity, scope),
                ).fetchall()
                for row in rows:
                    event = validate_original(row, self.ingest.access.instance_id)
                    if _digest(self._stored_links(db, row["receipt_id"])) != row["links_digest"]:
                        raise ValueError("lineage_links_invalid")
                    originals.add(PREFIX + row["receipt_id"])
                    outputs.append((row["receipt_id"], identity))
                    originals.update(
                        context_parents(db, stored_context(db, row["receipt_id"]), event)
                    )
                    if len(originals) > 32:
                        break
                if len(originals) > 32:
                    break
            graph = ReconcileGraph(
                db, self.ingest.access.instance_id, scope, self._stored_links, _digest
            )
            originals.update(graph.original_parents(outputs))
            originals.update(ChatGraph(graph).original_parents(outputs))
        return tuple(sorted(originals)[:33])

    async def enrich(self, record):
        if record is None:
            return None
        originals = await _joined_thread(self._references, record)
        references = tuple(dict.fromkeys((*originals, *record.source_raw_ids)))
        return record.model_copy(update={"source_raw_ids": references})

    def inventory(self, namespace, nodes):
        """Merge complete scoped relation sets, not the display reference limit."""
        from cloud_memory_hub.governance_inventory import MAX_CONTENT_BYTES, MAX_EDGES, MAX_RECORDS

        self.governance.require_ready()
        prefix = f"cmh:{self.ingest.access.instance_id}:"
        if not namespace.startswith(prefix):
            raise GovernanceError("governance_scope_mismatch")
        records = dict(nodes)
        used_bytes = sum(len(node.content.encode()) for node in records.values())
        used_edges = sum(len(node.parents) + len(node.versions) for node in records.values())
        with self.ingest._database() as db:
            self._check(db)
            count, size = db.execute(
                "SELECT count(*),COALESCE(sum(length(CAST(payload_json AS BLOB)) + "
                "COALESCE(length(CAST(context_json AS BLOB)),0) + "
                "COALESCE(length(CAST(inputs_json AS BLOB)),0) + "
                "COALESCE(length(CAST(chat_json AS BLOB)),0)),0) "
                "FROM cmh_event_receipts LEFT JOIN cmh_event_contexts USING(receipt_id) "
                "LEFT JOIN cmh_event_inputs USING(receipt_id) "
                "LEFT JOIN cmh_event_chat_inputs USING(receipt_id) "
                "WHERE scope_id=?",
                (namespace[len(prefix) :],),
            ).fetchone()
            if count > MAX_RECORDS or size > MAX_CONTENT_BYTES:
                raise GovernanceError("governance_scan_limit")
            rows = db.execute(
                "SELECT receipt.*,outcome.links_digest FROM cmh_event_receipts receipt "
                "LEFT JOIN cmh_event_outcomes outcome USING(receipt_id) "
                "WHERE receipt.scope_id=? ORDER BY receipt.rowid LIMIT ?",
                (namespace[len(prefix) :], MAX_RECORDS + 1),
            ).fetchall()
            for row in rows:
                event = validate_original(row, self.ingest.access.instance_id)
                original = PREFIX + row["receipt_id"]
                if original in records:
                    raise GovernanceError("governance_record_identity_mismatch")
                used_bytes += len(row["payload_json"].encode())
                if len(records) >= MAX_RECORDS or used_bytes > MAX_CONTENT_BYTES:
                    raise GovernanceError("governance_scan_limit")
                records[original] = RelationNode(
                    original,
                    "raw",
                    row["payload_json"],
                    "accepted_original",
                    "retained",
                )
                trace = trace_id(row["request_id"])
                if trace in records:
                    records[trace] = replace(records[trace], parents=frozenset({original}))
                    used_edges += 1
                links = self._stored_links(db, row["receipt_id"])
                context = stored_context(db, row["receipt_id"])
                parents = context_parents(db, context, event)
                used_bytes += len(context.encode()) if context is not None else 0
                if used_bytes > MAX_CONTENT_BYTES:
                    raise GovernanceError("governance_scan_limit")
                used_edges += len(parents) + len(links) * (1 + len(parents))
                if used_edges > MAX_EDGES:
                    raise GovernanceError("governance_scan_limit")
                if row["links_digest"] is None:
                    if links or context is not None:
                        raise GovernanceError("governance_lineage_invalid")
                    continue
                if _digest(links) != row["links_digest"]:
                    raise GovernanceError("governance_lineage_invalid")
                if context is not None:
                    if context.chain_status == "unavailable":
                        raise GovernanceError("governance_chain_capture_incomplete")
                    used_edges += len(context.chain_round_ids)
                    if used_edges > MAX_EDGES:
                        raise GovernanceError("governance_scan_limit")
                    for native in context.chain_round_ids:
                        identity = chain_id("r", native)
                        node = records.get(identity)
                        if node is None or node.kind != "raw" or node.layer != ROUND_LAYER:
                            raise GovernanceError("governance_reference_unresolved")
                        records[identity] = replace(node, parents=node.parents | {original})
                for kind, identity in links:
                    if identity not in records or records[identity].kind != kind:
                        raise GovernanceError("governance_reference_unresolved")
                    node = records[identity]
                    records[identity] = replace(node, parents=node.parents | {original} | parents)
            graph = ReconcileGraph(
                db,
                self.ingest.access.instance_id,
                namespace[len(prefix) :],
                self._stored_links,
                _digest,
            )
            receipts = [row["receipt_id"] for row in rows if row["links_digest"] is not None]
            records = graph.merge(records, receipts)
            records = ChatGraph(graph).merge(records, receipts)
        return records
