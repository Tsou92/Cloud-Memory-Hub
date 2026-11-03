"""Resolve immutable candidate versions through their producing Hub requests."""

from collections import deque
from dataclasses import replace

from hy_memory.coding.record_origin import verified_origin
from hy_memory.utils.source_filter import normalize_source

from cloud_memory_hub.governance_journal import GovernanceError
from cloud_memory_hub.governance_plan import RelationNode
from cloud_memory_hub.memory_evidence import stored_memory
from cloud_memory_hub.reconcile_evidence import encode
from cloud_memory_hub.source_history import PREFIX as ORIGINAL

PREFIX = "cmhm:"


class MemoryGraph:
    def __init__(self, graph, chat):
        self.graph, self.chat = graph, chat
        self.captures, self.nodes = {}, {}
        self.edges, self.bytes = 0, 0

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

    def build(self, initial):
        pending = deque(initial)
        graph = self.graph
        while pending:
            identity = pending.popleft()
            if identity in self.nodes:
                continue
            receipt = identity[len(PREFIX) :]
            capture = self.capture(receipt)
            if capture is None or not capture["stages"]:
                raise GovernanceError("governance_memory_stage_missing")
            parents = {ORIGINAL + receipt}
            for item in capture["stages"][0]["inputs"]:
                parents.update(self.producer(item, receipt))
            self.nodes[identity] = RelationNode(
                identity,
                "raw",
                encode(capture),
                "memory_reconcile_input",
                "retained",
                frozenset(parents),
            )
            self.edges += len(parents)
            self.bytes += len(self.nodes[identity].content.encode())
            if (
                len(self.nodes) > graph.record_limit
                or self.edges > graph.edge_limit
                or self.bytes + graph.bytes > graph.byte_limit
            ):
                raise GovernanceError("governance_scan_limit")
            pending.extend(parent for parent in parents if parent.startswith(PREFIX))
        graph._acyclic(self.nodes)
        return self.nodes

    def original_parents(self, outputs):
        roots = set()
        for receipt, target in outputs:
            roots.update(self.output_parents(receipt, target))
        nodes = self.build(roots)
        parents = {parent for node in nodes.values() for parent in node.parents}
        for node in self.chat.nodes.values():
            if node is not None and node.record_id in parents:
                parents.update(node.parents)
        return {parent for parent in parents if parent.startswith(ORIGINAL)}

    def merge(self, records, receipts):
        roots = set()
        for receipt in receipts:
            capture = self.capture(receipt)
            if capture is None or not capture["stages"]:
                continue
            roots.add(PREFIX + receipt)
            for kind, target in self.graph.entry(receipt)["links"]:
                if kind == "memory":
                    node = records[target]
                    records[target] = replace(
                        node, parents=node.parents | self.output_parents(receipt, target)
                    )
        additions = self.build(roots)
        if records.keys() & additions.keys():
            raise GovernanceError("governance_record_identity_mismatch")
        records.update(additions)
        # ChatGraph adds the referenced history stages and checks combined limits/cycles.
        return records
