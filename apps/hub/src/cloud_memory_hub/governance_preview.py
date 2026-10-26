"""Administrator-only, read-only impact observation; not an execution plan."""

import asyncio
import json
from collections.abc import Callable

from one_memory.hy_memory_bridge import HyMemoryBridge, _joined_thread
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from cloud_memory_hub.governance_authority import GovernanceAuthority
from cloud_memory_hub.governance_inventory import observe_inventory
from cloud_memory_hub.governance_journal import GovernanceError
from cloud_memory_hub.governance_plan import PreviewRequest, plan_relations
from cloud_memory_hub.privacy import CredentialFilter, PrivacyError
from cloud_memory_hub.remote_access import AccessError

TEXT_PREVIEW = 512
RESPONSE_BYTES = 192 * 1024


class GovernancePreview:
    def __init__(
        self,
        authority: GovernanceAuthority,
        bridge: Callable[[], HyMemoryBridge],
        privacy: CredentialFilter,
    ):
        self.authority = authority
        self.bridge = bridge
        self.privacy = privacy
        self._slots = asyncio.Semaphore(1)

    def _require_scope(self, scope):
        self.authority.require_ready()
        with self.authority.access._connection() as db:
            if not db.execute("SELECT 1 FROM projects WHERE project_id=?", (scope,)).fetchone():
                raise AccessError("governance_scope_not_found", 404)

    async def preview(self, request: PreviewRequest) -> dict:
        try:
            request = PreviewRequest.model_validate(request)
        except ValidationError:
            raise AccessError("invalid_request", 422) from None
        try:
            await asyncio.wait_for(self._slots.acquire(), timeout=0.1)
        except TimeoutError:
            raise AccessError("governance_preview_busy", 503) from None
        try:
            await run_in_threadpool(self._require_scope, request.scope_id)
            namespace = f"cmh:{self.authority.access.instance_id}:{request.scope_id}"
            nodes = await observe_inventory(self.bridge(), namespace)
            result = await _joined_thread(lambda: self._format(request, nodes))
            await run_in_threadpool(self.authority.require_ready)
            return result
        except GovernanceError as error:
            code = str(error)
            status = 404 if code == "governance_selection_not_found" else 503
            if code in {"governance_reference_unresolved", "governance_scan_limit"}:
                status = 409
            raise AccessError(code, status) from None
        except PrivacyError:
            raise AccessError("governance_preview_requires_refiltering", 503) from None
        except AccessError:
            raise
        except Exception:
            raise AccessError("governance_preview_unavailable", 503) from None
        finally:
            self._slots.release()

    def _format(self, request, nodes):
        plan = plan_relations(nodes, request.record_ids)
        # Bind every observed record, including ones outside the current closure:
        # a newly added descendant must invalidate pagination over an older view.
        mac = self.authority.journal._mac
        records = [
            mac(
                "preview-record-v1",
                [
                    identity,
                    node.kind,
                    node.content,
                    node.layer,
                    node.status,
                    sorted(node.parents),
                    sorted(node.versions),
                ],
            )
            for identity, node in sorted(nodes.items())
        ]
        digest = mac(
            "preview-view-v1",
            [
                request.scope_id,
                request.action,
                list(request.record_ids),
                records,
            ],
        )
        if request.expected_view is not None and request.expected_view != digest:
            raise AccessError("governance_preview_changed", 409)
        selected = plan["record_ids"]
        if request.offset > len(selected):
            raise AccessError("governance_offset_invalid", 422)
        raw_count = sum(nodes[identity].kind == "raw" for identity in selected)
        unresolved = set(plan["unresolved_origins"])
        end = request.offset + request.limit
        items = []
        for identity in selected[request.offset : end]:
            node = nodes[identity]
            # Filter before truncating; otherwise a secret can straddle the cut.
            content = self.privacy.protect({"content": node.content}).value["content"]
            effect = (
                "remove_hub_content"
                if request.action == "purge"
                else (
                    "retain_for_explicit_read" if node.kind == "raw" else "suppress_automatic_use"
                )
            )
            items.append(
                {
                    "record_id": identity,
                    "kind": node.kind,
                    "layer": node.layer,
                    "status": node.status,
                    "content": content[:TEXT_PREVIEW],
                    "content_truncated": len(content) > TEXT_PREVIEW,
                    "reasons": list(plan["reasons"][identity]),
                    "unresolved_origin": identity in unresolved,
                    "proposed_effect": effect,
                }
            )
        result = {
            "scope_id": request.scope_id,
            "action": request.action,
            "view_digest": digest,
            "consistency": "observational",
            "coverage": "explicit_stored_relations",
            "execution_available": False,
            "content_trust": "untrusted_history",
            "total": len(selected),
            "raw_count": raw_count,
            "memory_count": len(selected) - raw_count,
            "root_count": len(plan["roots"]),
            "unresolved_origin_count": len(unresolved),
            "items": items,
            "next_offset": end if end < len(selected) else None,
        }
        metadata = {
            **result,
            "items": [{k: v for k, v in item.items() if k != "content"} for item in items],
        }
        if self.privacy.protect(metadata).value != metadata:
            raise AccessError("governance_metadata_requires_review", 503)
        if len(json.dumps(result, ensure_ascii=False).encode()) > RESPONSE_BYTES:
            raise AccessError("governance_response_too_large", 503)
        return result
