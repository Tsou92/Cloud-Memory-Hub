"""Resolve profile versions inside the Hub user namespace, across its Agents."""

from hy_memory.coding.record_origin import verified_origin

from cloud_memory_hub.governance_journal import GovernanceError
from cloud_memory_hub.profile_evidence import LAYER, PREFIX, stored_profile
from cloud_memory_hub.source_history import PREFIX as ORIGINAL
from cloud_memory_hub.version_input_graph import VersionInputGraph


class ProfileGraph(VersionInputGraph):
    prefix, layer = PREFIX, "basic_profile_input"

    def __init__(self, graph, chat, live_origins=None):
        super().__init__(graph, chat)
        self.live_origins = live_origins or {}

    def capture(self, receipt):
        if receipt in self.captures:
            return self.captures[receipt]
        graph = self.graph
        entry = graph.entry(receipt)
        row = graph.db.execute(
            "SELECT length(CAST(profile_json AS BLOB)) FROM cmh_event_profile_inputs "
            "WHERE receipt_id=?",
            (receipt,),
        ).fetchone()
        if row is None:
            raise GovernanceError("governance_profile_capture_missing")
        graph.bytes += row[0]
        if graph.bytes > graph.byte_limit:
            raise GovernanceError("governance_scan_limit")
        capture = stored_profile(graph.db, receipt, entry["request"])["capture"]
        if capture is not None and (
            capture["status"] == "unavailable" or not capture["effects_complete"]
        ):
            raise GovernanceError("governance_profile_capture_incomplete")
        self.captures[receipt] = capture
        return capture

    def outputs(self, receipt):
        capture = self.capture(receipt)
        return set(capture["output_ids"]) if capture is not None else set()

    def stage_parents(self, receipt):
        parents = super().stage_parents(receipt)
        history = self.chat.node(receipt)
        if history is None:
            raise GovernanceError("governance_profile_history_unobserved")
        parents.add(history.record_id)
        return parents

    def output_parents(self, receipt, target):
        if (
            self.graph.db.execute(
                "SELECT 1 FROM cmh_event_profile_outputs WHERE receipt_id=? AND output_id=?",
                (receipt, target),
            ).fetchone()
            is None
        ):
            return set()
        if target in self.outputs(receipt):
            return {PREFIX + receipt}
        return set()

    def producer(self, item, consumer):
        graph = self.graph
        record = item["record"]
        receipt = graph.producer_receipt(
            verified_origin("profile", record), consumer, error="governance_profile"
        )
        request = graph.entry(receipt)["request"]
        if (
            record["user_id"] != request.user_id
            or record["agent_id"] not in (None, "")
            or record["session_id"] not in (None, "")
            or record["source"] != "default"
        ):
            raise GovernanceError("governance_profile_scope_mismatch")
        if item["id"] not in self.outputs(receipt):
            raise GovernanceError("governance_profile_output_unproven")
        return (
            {ORIGINAL + receipt}
            | graph.entry(receipt)["context"]
            | self.output_parents(receipt, item["id"])
        )

    def merge(self, records, receipts):
        proven = set()
        for receipt in receipts:
            for identity in self.outputs(receipt):
                node = records.get(identity)
                if node is None or node.kind != "memory" or node.layer != LAYER:
                    raise GovernanceError("governance_profile_output_unproven")
                if (
                    self.live_origins.get(identity)
                    != self.graph.entry(receipt)["request"].request_id
                ):
                    raise GovernanceError("governance_profile_output_changed")
                if identity in proven:
                    raise GovernanceError("governance_profile_output_duplicate")
                proven.add(identity)
        if any(
            node.layer == LAYER and identity not in proven for identity, node in records.items()
        ):
            raise GovernanceError("governance_profile_output_unobserved")
        return super().merge(records, receipts)
