"""Connect authenticated intake to the upstream SDK's durable generation gate."""

import json

from one_memory.hy_memory_bridge import _joined_thread
from one_memory.memory_processing import sdk_generation_input_for

from cloud_memory_hub.governance_journal import GovernanceError
from cloud_memory_hub.governance_sources import (
    content_policy_target,
    content_targets,
    source_target,
)
from cloud_memory_hub.remote_access import AccessError
from cloud_memory_hub.remote_ingest import write_request
from cloud_memory_hub.write_admissions import WriteAdmissions


class SdkGeneration:
    def __init__(self, authority, suppressions):
        self.authority, self.suppressions = authority, suppressions
        self.admissions = WriteAdmissions(authority.access, authority.journal)

    def enroll(self, db, event):
        journal = self.authority.journal
        prepared = {}

        def prepare(request_id):
            request = write_request(event, request_id=request_id)
            prepared[request_id] = request
            return sdk_generation_input_for(request)

        binding = self.admissions.enroll_in(
            db,
            event,
            prepare,
            {content_policy_target(journal, event.scope_id), source_target(journal, event)}
            | content_targets(journal, event),
        )
        return prepared[binding.request_id]

    def _require_allowed(self, binding):
        # The caller already holds SDK operation admission. Governance cannot
        # append a new rule until this permit decision has finished. These
        # readers never borrow another access-store writer.
        targets, unavailable = self.suppressions._rules(binding.project_id)
        matched = set(binding.rule_targets) & targets
        if any(target.kind == "source_record" for target in matched):
            raise AccessError("governance_source_suppressed", 409)
        if unavailable is not None:
            raise AccessError(unavailable, 503)
        policy = content_policy_target(self.authority.journal, binding.project_id)
        if matched - {policy}:
            raise AccessError("governance_content_suppressed", 409)

    def _permit(self, actual):
        self.authority.require_writes_ready()
        # Historical requests keep their prior execution contract. They never
        # acquire a birth certificate or the resulting negative-effect proof.
        if not actual.request_id.startswith("cmh2:"):
            self._require_legacy_allowed(actual)
            return
        self.admissions.permit_once(actual, require_allowed=self._require_allowed)

    def _require_legacy_allowed(self, actual):
        try:
            value = json.loads(actual.canonical_input)
            namespace = value["kwargs"]["user_id"]
            prefix = f"cmh:{self.authority.access.instance_id}:"
            if not isinstance(namespace, str):
                raise ValueError
            if not namespace.startswith(prefix):
                return
            scope = namespace[len(prefix) :]
        except (ValueError, TypeError, KeyError):
            raise GovernanceError("governance_admission_input_mismatch") from None
        # A legacy restored job cannot establish that its previous invocation
        # or captured inputs predate a deletion. Keep that project's evidence
        # until the positive-effect recovery path can certify it.
        if self.suppressions.business_plans(scope):
            raise GovernanceError("governance_admission_legacy_scope_unproven")

    async def permit(self, actual):
        # Cancellation joins the persistence worker before releasing admission.
        await _joined_thread(self._permit, actual)
