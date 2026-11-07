"""Prove captured raw bodies against accepted originals, not current mutable rows."""

from dataclasses import replace

from hy_memory.coding.preproc import strip_tool_messages
from hy_memory.pipelines.base import ChatMessage, WriteRequest
from hy_memory.utils.source_filter import normalize_source

from cloud_memory_hub.chat_evidence import stored_chat
from cloud_memory_hub.governance_journal import GovernanceError
from cloud_memory_hub.governance_plan import RelationNode
from cloud_memory_hub.purged_evidence import purged
from cloud_memory_hub.reconcile_evidence import encode
from cloud_memory_hub.source_history import PREFIX as ORIGINAL

PREFIX = "cmhj:"


class ChatGraph:
    def __init__(self, graph):
        # Share the validated receipt cache and total scan budget with reconciliation.
        self.graph, self.nodes, self.producers = graph, {}, {}

    def producer(self, item, consumer):
        graph = self.graph
        identity = item["node_id"]
        if identity not in self.producers:
            rows = graph.db.execute(
                "SELECT receipt_id FROM cmh_event_output_links "
                "WHERE kind='raw' AND output_id=? LIMIT 2",
                (identity,),
            ).fetchall()
            if len(rows) != 1:
                raise GovernanceError("governance_chat_origin_unproven")
            self.producers[identity] = rows[0][0]
        receipt = self.producers[identity]
        if receipt == consumer:
            raise GovernanceError("governance_chat_self_reference")
        entry = graph.entry(receipt)
        request = entry["request"]
        if ("raw", identity) not in entry["links"]:
            raise GovernanceError("governance_chat_origin_unproven")
        if (item["user_id"], item["agent_id"], item["session_id"], item["source"]) != (
            request.user_id,
            request.agent_id or "default_agent",
            request.session_id or "default_session",
            normalize_source(request.source_id),
        ) or (item["request_id"] is not None and item["request_id"] != request.request_id):
            raise GovernanceError("governance_chat_identity_mismatch")
        # Full input (e.g. lite) and native chat fallback are the two possible
        # L1 renderings. Require exact equality to an upstream-produced form.
        messages = [ChatMessage.from_dict(turn.model_dump()) for turn in request.turns]
        bodies = {
            WriteRequest(messages=messages).get_flat_content(),
            WriteRequest(messages=strip_tool_messages(messages)).get_flat_content(),
        }
        if item["content"] not in bodies:
            raise GovernanceError("governance_chat_content_mismatch")
        return ORIGINAL + receipt

    def node(self, receipt):
        if receipt in self.nodes:
            return self.nodes[receipt]
        graph = self.graph
        entry = graph.entry(receipt)
        row = graph.db.execute(
            "SELECT length(CAST(chat_json AS BLOB)) FROM cmh_event_chat_inputs WHERE receipt_id=?",
            (receipt,),
        ).fetchone()
        if row is None:
            if purged(graph.db, PREFIX, receipt):
                self.nodes[receipt] = None
                return None
            raise GovernanceError("governance_chat_capture_missing")
        graph.bytes += row[0]
        if graph.bytes > graph.byte_limit:
            raise GovernanceError("governance_scan_limit")
        value = stored_chat(graph.db, receipt, entry["request"])
        capture = value["capture"]
        if capture is None:
            self.nodes[receipt] = None  # Older outcome remains explicitly unobserved.
            return None
        if capture["status"] == "unavailable":
            raise GovernanceError("governance_chat_capture_incomplete")
        parents = {ORIGINAL + receipt}
        for item in capture["records"]:
            parents.add(self.producer(item, receipt))
        node = RelationNode(
            PREFIX + receipt,
            "raw",
            encode(capture),
            "chat_history_input",
            "retained",
            frozenset(parents),
        )
        self.nodes[receipt] = node
        return node

    def output_parent(self, receipt, target):
        entry = self.graph.entry(receipt)
        if ("memory", target) not in entry["links"]:
            return None
        return self.node(receipt)

    def original_parents(self, outputs):
        parents = set()
        for receipt, target in outputs:
            node = self.output_parent(receipt, target)
            if node is not None:
                parents.update(node.parents)
        return parents

    def merge(self, records, receipts):
        graph = self.graph
        for receipt in receipts:
            node = self.node(receipt)
            if node is None:
                continue
            if node.record_id in records:
                raise GovernanceError("governance_record_identity_mismatch")
            records[node.record_id] = node
            for kind, target in graph.entry(receipt)["links"]:
                if kind == "memory":
                    output = records[target]
                    records[target] = replace(output, parents=output.parents | {node.record_id})
            if any(parent not in records for parent in node.parents):
                raise GovernanceError("governance_reference_unresolved")
        if (
            len(records) > graph.record_limit
            or sum(len(node.parents) + len(node.versions) for node in records.values())
            > graph.edge_limit
            or sum(len(node.content.encode()) for node in records.values()) + graph.bytes
            > graph.byte_limit
        ):
            raise GovernanceError("governance_scan_limit")
        graph._acyclic(records)
        return records
