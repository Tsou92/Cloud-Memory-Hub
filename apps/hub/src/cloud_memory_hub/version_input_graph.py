"""Bounded graph traversal for immutable native candidate versions."""

from collections import deque
from dataclasses import replace

from cloud_memory_hub.governance_journal import GovernanceError
from cloud_memory_hub.governance_plan import RelationNode
from cloud_memory_hub.reconcile_evidence import encode
from cloud_memory_hub.source_history import PREFIX as ORIGINAL


class VersionInputGraph:
    def __init__(self, graph, chat):
        self.graph, self.chat = graph, chat
        self.captures, self.nodes = {}, {}
        self.edges, self.bytes = 0, 0

    def stage_parents(self, receipt):
        return {ORIGINAL + receipt}

    def build(self, initial):
        pending = deque(initial)
        graph = self.graph
        while pending:
            identity = pending.popleft()
            if identity in self.nodes:
                continue
            receipt = identity[len(self.prefix) :]
            capture = self.capture(receipt)
            if capture is None or not capture["stages"]:
                raise GovernanceError("governance_memory_stage_missing")
            parents = self.stage_parents(receipt)
            for item in capture["stages"][0]["inputs"]:
                parents.update(self.producer(item, receipt))
            self.nodes[identity] = RelationNode(
                identity,
                "raw",
                encode(capture),
                self.layer,
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
            pending.extend(parent for parent in parents if parent.startswith(self.prefix))
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
            roots.add(self.prefix + receipt)
            for target in self.outputs(receipt):
                if target not in records or records[target].kind != "memory":
                    raise GovernanceError("governance_reference_unresolved")
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
