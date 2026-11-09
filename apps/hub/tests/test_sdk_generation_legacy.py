"""FR-10/FR-11: legacy SDK permits follow actual project, without new births."""

import asyncio
import json
from uuid import uuid4

import pytest
from cloud_memory_hub.governance_execution import ExecutionStore
from cloud_memory_hub.governance_journal import GovernanceError, Intent
from cloud_memory_hub.governance_sources import capture_rules
from one_memory.hy_memory_bridge import HyMemoryBridgeError, SdkGenerationInput
from test_original_archive import config_for
from test_output_lineage import fixture
from test_sdk_generation_admission import generation_request, intake, proof, sdk_seam


def sign_forget(app, event, job):
    authority = app.core.state.cmh_governance
    store = ExecutionStore(authority.journal)
    records = {"cmhe:" + job: "raw"}
    rules = capture_rules(authority.journal, app.core.state.cmh_ingest, event["scope_id"], records)
    intent = Intent(
        operation_id=str(uuid4()),
        actor_id=authority.access.instance_id,
        scope_id=event["scope_id"],
        action="forget",
        targets=(authority.journal.target(event["scope_id"], "raw", "cmhe:" + job), *rules),
    )
    store.plan(intent, records)
    authority.append(intent)
    store.complete(intent)


def legacy_request_id(event):
    return f"cmh:{event['instance_id']}:{event['device_id']}:{event['event_id']}"


@pytest.mark.parametrize("identity", ["legacy", "non_hub"])
def test_actual_governed_project_blocks_legacy_but_other_project_can_invoke(
    tmp_path, monkeypatch, identity
):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            _, _, event_a, job_a, frozen_a = await intake(app, web, config)
            _, _, event_b, _, frozen_b = await intake(app, web, config)
            assert event_a["scope_id"] != event_b["scope_id"]
            sign_forget(app, event_a, job_a)
            admissions = app.core.state.cmh_generation.admissions
            scopes_before = admissions.journal.scopes()
            births_before = (proof(app, frozen_a), proof(app, frozen_b))
            assert len(scopes_before) == 2
            assert all(item.state == "enrolled" for item in births_before)
            bridge, calls = await sdk_seam(app, monkeypatch)

            request_a = generation_request(frozen_a).model_copy(
                update={
                    "request_id": (
                        legacy_request_id(event_a)
                        if identity == "legacy"
                        else "synthetic-non-hub-request"
                    )
                }
            )
            assert request_a.user_id == f"cmh:{event_a['instance_id']}:{event_a['scope_id']}"
            with pytest.raises(HyMemoryBridgeError, match="^hy_memory_generation_not_permitted$"):
                await bridge.ingest(request_a)
            assert calls == []

            request_b = generation_request(frozen_b).model_copy(
                update={"request_id": legacy_request_id(event_b)}
            )
            # The synthetic SDK raises after entry; this proves admission only,
            # without performing extraction or calling any external model.
            with pytest.raises(HyMemoryBridgeError, match="^hy_memory_runtime_failed$"):
                await bridge.ingest(request_b)
            assert len(calls) == 1
            assert calls[0][1]["user_id"] == f"cmh:{event_b['instance_id']}:{event_b['scope_id']}"
            assert calls[0][1]["request_id"] == legacy_request_id(event_b)
            assert admissions.journal.scopes() == scopes_before
            assert (proof(app, frozen_a), proof(app, frozen_b)) == births_before

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "canonical",
    [{}, {"kwargs": {}}, {"kwargs": {"user_id": None}}, {"kwargs": {"user_id": 7}}],
    ids=["missing_kwargs", "missing_user", "null_user", "numeric_user"],
)
def test_malformed_legacy_input_has_fixed_refusal_and_preserves_birth(tmp_path, canonical):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            _, _, _, _, frozen = await intake(app, web, config)
            generation = app.core.state.cmh_generation
            before = proof(app, frozen)
            scopes_before = generation.admissions.journal.scopes()
            actual = SdkGenerationInput(
                operation="add",
                request_id="synthetic-legacy-request",
                canonical_input=json.dumps(canonical).encode(),
            )
            with pytest.raises(GovernanceError, match="^governance_admission_input_mismatch$"):
                await generation.permit(actual)
            assert proof(app, frozen) == before
            assert before.state == "enrolled"
            assert generation.admissions.journal.scopes() == scopes_before

    asyncio.run(scenario())
