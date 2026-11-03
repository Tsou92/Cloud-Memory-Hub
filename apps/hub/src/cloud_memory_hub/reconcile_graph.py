"""Follow captured producer requests without linking through mutable memory IDs."""

from collections import deque
from dataclasses import replace

from hy_memory.coding.record_origin import verified_origin
from hy_memory.utils.source_filter import normalize_source
from one_memory.memory_processing import MemoryTaskRunner

from cloud_memory_hub.governance_journal import GovernanceError
from cloud_memory_hub.governance_plan import RelationNode
from cloud_memory_hub.input_context import context_parents, stored_context
from cloud_memory_hub.original_archive import validate_original
from cloud_memory_hub.productivity_read import parse_id, record_id
from cloud_memory_hub.reconcile_evidence import STAGES, encode, stored_inputs
from cloud_memory_hub.remote_ingest import write_request
from cloud_memory_hub.source_history import PREFIX as ORIGINAL

PREFIX = "cmhi:"


def stage_id(receipt, index):
    identity = f"{PREFIX}{receipt}:{index}"
    if len(identity) > 200 or type(index) is not int or index < 0:
        raise GovernanceError("governance_input_identity_invalid")
    return identity


class ReconcileGraph:
    def __init__(self, db, instance, scope, links, digest):
        from cloud_memory_hub.governance_inventory import MAX_CONTENT_BYTES, MAX_EDGES, MAX_RECORDS

        self.db, self.instance, self.scope = db, instance, scope
        self.links, self.digest = links, digest
        self.entries, self.producers = {}, {}
        self.byte_limit, self.edge_limit, self.record_limit = (
            MAX_CONTENT_BYTES,
            MAX_EDGES,
            MAX_RECORDS,
        )
        self.bytes = 0

    def entry(self, receipt):
        if receipt in self.entries:
            return self.entries[receipt]
        size = self.db.execute(
            "SELECT length(CAST(payload_json AS BLOB)) + "
            "COALESCE(length(CAST(context_json AS BLOB)),0) + "
            "COALESCE(length(CAST(inputs_json AS BLOB)),0) "
            "FROM cmh_event_receipts LEFT JOIN cmh_event_contexts USING(receipt_id) "
            "LEFT JOIN cmh_event_inputs USING(receipt_id) WHERE receipt_id=? AND scope_id=?",
            (receipt, self.scope),
        ).fetchone()
        if size is None:
            raise GovernanceError("governance_input_original_missing")
        self.bytes += size[0]
        if self.bytes > self.byte_limit or len(self.entries) >= self.record_limit:
            raise GovernanceError("governance_scan_limit")
        row = self.db.execute(
            "SELECT receipt.*,outcome.links_digest FROM cmh_event_receipts receipt "
            "LEFT JOIN cmh_event_outcomes outcome USING(receipt_id) WHERE receipt_id=?",
            (receipt,),
        ).fetchone()
        event = validate_original(row, self.instance)
        if event.scope_id != self.scope:
            raise GovernanceError("governance_input_scope_mismatch")
        frozen = write_request(event)
        request = MemoryTaskRunner.request_for(frozen.idempotency_key, frozen.input_records)
        links = self.links(self.db, receipt)
        if row["links_digest"] is None or self.digest(links) != row["links_digest"]:
            raise GovernanceError("governance_input_output_unproven")
        inputs = stored_inputs(self.db, receipt, request)
        if inputs is None:
            raise GovernanceError("governance_input_capture_missing")
        capture = inputs["capture"]
        if capture is not None and capture["status"] == "unavailable":
            raise GovernanceError("governance_input_capture_incomplete")
        if capture is not None:
            required = set()
            for kind, identity in links:
                typed = parse_id(identity)
                if kind != "memory" or typed is None or typed[0] != inputs["variant"]:
                    raise GovernanceError("governance_input_output_unproven")
                required.add("experience_decision" if typed[1] == "e" else "fact_decision")
            if not required <= {stage["stage"] for stage in capture["stages"]}:
                raise GovernanceError("governance_input_capture_incomplete")
        parents = context_parents(self.db, stored_context(self.db, receipt), event)
        entry = {
            "request": request,
            "links": links,
            "variant": inputs["variant"],
            "stages": capture["stages"] if capture is not None else [],
            "context": parents,
        }
        self.entries[receipt] = entry
        return entry

    def targets(self, entry, stage):
        if stage["status"] != "used":
            return frozenset()
        _, kind = STAGES[stage["stage"]]
        prefix = f"cmhp:{entry['variant']}:{kind}:"
        produced = frozenset(
            identity
            for tag, identity in entry["links"]
            if tag == "memory" and identity.startswith(prefix)
        )
        if stage["output_id"] is not None:
            return produced & {record_id(entry["variant"], kind, stage["output_id"])}
        return produced

    def output_parents(self, receipt, target):
        entry = self.entry(receipt)
        return frozenset(
            stage_id(receipt, index)
            for index, stage in enumerate(entry["stages"])
            if target in self.targets(entry, stage)
        )

    def producer_receipt(self, identity, consumer, *, error="governance_input"):
        """Resolve an already-checked semantic origin within the shared scan budget."""
        if identity is None:
            raise GovernanceError(error + "_origin_unproven")
        if identity not in self.producers:
            rows = self.db.execute(
                "SELECT receipt_id,scope_id FROM cmh_event_receipts WHERE request_id=? LIMIT 2",
                (identity,),
            ).fetchall()
            if len(rows) != 1:
                raise GovernanceError(error + "_original_unproven")
            if rows[0][1] != self.scope:
                raise GovernanceError(error + "_scope_mismatch")
            self.producers[identity] = rows[0][0]
        receipt = self.producers[identity]
        if receipt == consumer:
            raise GovernanceError(error + "_self_reference")
        return receipt

    def producer(self, item, variant, consumer):
        receipt = self.producer_receipt(verified_origin(item["kind"], item["record"]), consumer)
        entry = self.entry(receipt)
        request, record = entry["request"], item["record"]
        if (record["user_id"], record["agent_id"], record["source"]) != (
            request.user_id,
            request.agent_id,
            normalize_source(request.source_id),
        ) or (item["kind"] == "fact" and record["session_id"] != request.session_id):
            raise GovernanceError("governance_input_scope_mismatch")
        target = record_id(variant, "e" if item["kind"] == "experience" else "f", item["id"])
        if ("memory", target) not in entry["links"]:
            raise GovernanceError("governance_input_output_unproven")
        return {ORIGINAL + receipt} | entry["context"] | self.output_parents(receipt, target)

    def nodes(self, initial):
        pending, nodes, edges, size = deque(initial), {}, 0, 0
        while pending:
            identity = pending.popleft()
            if identity in nodes:
                continue
            receipt, raw_index = identity[len(PREFIX) :].rsplit(":", 1)
            index = int(raw_index)
            entry = self.entry(receipt)
            if stage_id(receipt, index) != identity or index >= len(entry["stages"]):
                raise GovernanceError("governance_input_identity_invalid")
            stage = entry["stages"][index]
            parents = {ORIGINAL + receipt}
            for item in stage["inputs"]:
                parents.update(self.producer(item, entry["variant"], receipt))
            node = RelationNode(
                identity, "raw", encode(stage), "reconcile_input", "retained", frozenset(parents)
            )
            size += len(node.content.encode())
            edges += len(parents)
            if (
                len(nodes) >= self.record_limit
                or edges > self.edge_limit
                or size + self.bytes > self.byte_limit
            ):
                raise GovernanceError("governance_scan_limit")
            nodes[identity] = node
            pending.extend(parent for parent in parents if parent.startswith(PREFIX))
        self._acyclic(nodes)
        return nodes

    @staticmethod
    def _acyclic(nodes):
        indegree, children = {}, {}
        for identity, node in nodes.items():
            parents = node.parents & nodes.keys()
            indegree[identity] = len(parents)
            for parent in parents:
                children.setdefault(parent, []).append(identity)
        pending, count = deque(key for key, degree in indegree.items() if not degree), 0
        while pending:
            identity = pending.popleft()
            count += 1
            for child in children.get(identity, ()):
                indegree[child] -= 1
                if not indegree[child]:
                    pending.append(child)
        if count != len(nodes):
            raise GovernanceError("governance_input_cycle")

    def original_parents(self, outputs):
        roots = set()
        for receipt, target in outputs:
            roots.update(self.output_parents(receipt, target))
        return frozenset(
            parent
            for node in self.nodes(roots).values()
            for parent in node.parents
            if parent.startswith(ORIGINAL)
        )

    def merge(self, records, receipts):
        roots = set()
        for receipt in receipts:
            entry = self.entry(receipt)
            roots.update(stage_id(receipt, index) for index in range(len(entry["stages"])))
            if len(roots) + len(records) > self.record_limit:
                raise GovernanceError("governance_scan_limit")
            for kind, target in entry["links"]:
                if target not in records or records[target].kind != kind:
                    raise GovernanceError("governance_reference_unresolved")
                node = records[target]
                records[target] = replace(
                    node, parents=node.parents | self.output_parents(receipt, target)
                )
        additions = self.nodes(roots)
        if records.keys() & additions.keys():
            raise GovernanceError("governance_record_identity_mismatch")
        records.update(additions)
        if (
            len(records) > self.record_limit
            or sum(len(node.parents) + len(node.versions) for node in records.values())
            > self.edge_limit
            or sum(len(node.content.encode()) for node in records.values()) + self.bytes
            > self.byte_limit
        ):
            raise GovernanceError("governance_scan_limit")
        for node in additions.values():
            if any(parent not in records for parent in node.parents):
                raise GovernanceError("governance_reference_unresolved")
        return records
