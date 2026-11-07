"""Resolve immutable candidate versions through their producing Hub requests."""

from hy_memory.coding.record_origin import verified_origin
from hy_memory.utils.source_filter import normalize_source

from cloud_memory_hub.governance_journal import GovernanceError
from cloud_memory_hub.memory_evidence import stored_memory
from cloud_memory_hub.purged_evidence import purged
from cloud_memory_hub.source_history import PREFIX as ORIGINAL
from cloud_memory_hub.version_input_graph import VersionInputGraph

PREFIX = "cmhm:"


class MemoryGraph(VersionInputGraph):
    prefix, layer = PREFIX, "memory_reconcile_input"

    def outputs(self, receipt):
        return {
            identity for kind, identity in self.graph.entry(receipt)["links"] if kind == "memory"
        }

    def stage_parents(self, receipt):
        parents = super().stage_parents(receipt)
        if self.capture(receipt)["stages"][0]["status"] != "skipped":
            history = self.chat.node(receipt)
            if history is not None:
                parents.add(history.record_id)
        return parents

    def capture(self, receipt):
        if receipt in self.captures:
            return self.captures[receipt]
        graph = self.graph
        entry = graph.entry(receipt)
        row = graph.db.execute(
            "SELECT length(CAST(memory_json AS BLOB)) FROM cmh_event_memory_inputs "
            "WHERE receipt_id=?",
            (receipt,),
        ).fetchone()
        if row is None:
            if purged(graph.db, PREFIX, receipt):
                self.captures[receipt] = None
                return None
            raise GovernanceError("governance_memory_capture_missing")
        graph.bytes += row[0]
        if graph.bytes > graph.byte_limit:
            raise GovernanceError("governance_scan_limit")
        capture = stored_memory(graph.db, receipt, entry["request"])["capture"]
        if capture is not None and capture["status"] == "unavailable":
            raise GovernanceError("governance_memory_capture_incomplete")
        self.captures[receipt] = capture
        return capture

    def output_parents(self, receipt, target):
        capture = self.capture(receipt)
        if (
            capture is not None
            and capture["stages"]
            and capture["stages"][0]["status"] == "used"
            and ("memory", target) in self.graph.entry(receipt)["links"]
        ):
            return {PREFIX + receipt}
        return set()

    def producer(self, item, consumer):
        graph = self.graph
        receipt = graph.producer_receipt(
            verified_origin("memory", item["record"]), consumer, error="governance_memory"
        )
        entry = graph.entry(receipt)
        request, record = entry["request"], item["record"]
        if (record["user_id"], record["agent_id"], record["source"]) != (
            request.user_id,
            request.agent_id,
            normalize_source(request.source_id),
        ):
            raise GovernanceError("governance_memory_scope_mismatch")
        if ("memory", item["id"]) not in entry["links"]:
            raise GovernanceError("governance_memory_output_unproven")
        if self.capture(receipt) is None:
            raise GovernanceError("governance_memory_origin_capture_unobserved")
        parents = {ORIGINAL + receipt} | entry["context"] | self.output_parents(receipt, item["id"])
        history = self.chat.output_parent(receipt, item["id"])
        if history is not None:
            parents.add(history.record_id)
        return parents
