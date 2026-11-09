"""Execute forget (ADR-0032 slice 1): durable suppression, not physical deletion.

State lives beside the journal in the governance directory and holds record IDs
only, never content. The journal intent is the authority; this table records
how far execution reached so a restart can finish it.
"""

import asyncio
import hmac
import json
import sqlite3
from contextlib import asynccontextmanager, contextmanager, nullcontext
from uuid import uuid4

from hy_memory.data.vector_store_chroma import _run_in_vdb_pool
from one_memory.hy_memory_bridge import _joined_thread
from pydantic import ConfigDict, Field, ValidationError, field_validator

from cloud_memory_hub.governance_inventory import observe_inventory, require_modeled_scope
from cloud_memory_hub.governance_journal import GovernanceError, Intent
from cloud_memory_hub.governance_plan import PreviewRequest, plan_relations
from cloud_memory_hub.governance_requests import request_targets, require_same_request
from cloud_memory_hub.governance_sources import MAX_TARGETS
from cloud_memory_hub.governance_sources import capture_rules as capture_sources
from cloud_memory_hub.remote_access import AccessError, DeviceAccessStore
from cloud_memory_hub.sdk_reconcile import RESERVED_PREFIXES

SCHEMA = 1
SUPPRESSED_STATUS = "archived"


def require_forget_supported(records):
    """Independent productivity stores still need internal-input suppression."""
    if any(identity.startswith(("cmhp:", "cmht:", "cmhc:", "cmhv:")) for identity in records):
        raise GovernanceError("governance_forget_kind_unsupported")


class ExecuteRequest(PreviewRequest):
    """The administrator confirms exactly the view they reviewed."""

    model_config = ConfigDict(revalidate_instances="always")
    action: str = Field(pattern=r"^(forget|purge)$")
    expected_view: str = Field(pattern=r"^[0-9a-f]{64}$")
    operation_id: str | None = None

    @field_validator("operation_id")
    @classmethod
    def operation(cls, value):
        from cloud_memory_hub.events import canonical_uuid

        return None if value is None else canonical_uuid(value)


class ExecutionStore:
    """planned (IDs saved, intent maybe not yet appended) -> completed."""

    def __init__(self, journal):
        self.journal = journal
        self.path = journal.path.with_name("execution.sqlite3")

    def _sign(self, values):
        return self.journal._mac("execution-v1", values)

    @contextmanager
    def _connection(self, *, writing=False):
        try:
            DeviceAccessStore._check_private(self.path.parent, directory=True)
            if writing and not self.path.exists():
                self.path.touch(mode=0o600, exist_ok=False)
            DeviceAccessStore._check_private(self.path)
            db = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=10)
            db.row_factory = sqlite3.Row
            try:
                db.execute("PRAGMA synchronous=FULL")
                db.execute("BEGIN IMMEDIATE" if writing else "BEGIN")
                version = db.execute("PRAGMA user_version").fetchone()[0]
                if version == 0 and writing:
                    db.execute(
                        "CREATE TABLE operations (operation_id TEXT PRIMARY KEY, "
                        "scope_id TEXT NOT NULL, intent TEXT NOT NULL, "
                        "state TEXT NOT NULL CHECK(state IN ('planned','completed')), "
                        "signature TEXT NOT NULL)"
                    )
                    db.execute(
                        "CREATE TABLE suppressed (operation_id TEXT NOT NULL "
                        "REFERENCES operations(operation_id), scope_id TEXT NOT NULL, "
                        "record_id TEXT NOT NULL, kind TEXT NOT NULL, signature TEXT NOT NULL, "
                        "PRIMARY KEY(operation_id, record_id))"
                    )
                    db.execute("CREATE INDEX suppressed_scope ON suppressed(scope_id, record_id)")
                    db.execute(f"PRAGMA user_version={SCHEMA}")
                elif version != SCHEMA:
                    raise GovernanceError("governance_execution_version_unsupported")
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise
            finally:
                db.close()
        except GovernanceError:
            raise
        except Exception:
            raise GovernanceError("governance_execution_unavailable") from None

    def _check(self, row, table):
        values = [row[key] for key in row.keys() if key != "signature"]
        if not hmac.compare_digest(row["signature"], self._sign([table, *values])):
            raise GovernanceError("governance_execution_tampered")

    def _rows(self, query, values=()):
        if not self.path.exists():
            return []
        with self._connection() as db:
            rows = db.execute(query, values).fetchall()
            table = "operations" if "FROM operations" in query else "suppressed"
            for row in rows:
                self._check(row, table)
            return rows

    def operations(self):
        if not self.path.exists():
            return {}
        # One snapshot: a missing signed row is as unsafe as an edited one.
        with self._connection() as db:
            operations = db.execute("SELECT * FROM operations").fetchall()
            suppressed = db.execute("SELECT * FROM suppressed").fetchall()
        result, actual = {}, {}
        for row in operations:
            self._check(row, "operations")
            intent = Intent.model_validate_json(row["intent"])
            if row["operation_id"] != intent.operation_id or row["scope_id"] != intent.scope_id:
                raise GovernanceError("governance_execution_tampered")
            result[intent.operation_id] = (row["state"], intent)
            actual[intent.operation_id] = set()
        for row in suppressed:
            self._check(row, "suppressed")
            known = result.get(row["operation_id"])
            if known is None or row["scope_id"] != known[1].scope_id:
                raise GovernanceError("governance_execution_tampered")
            actual[row["operation_id"]].add(
                self.journal.target(row["scope_id"], row["kind"], row["record_id"])
            )
        for operation, (_, intent) in result.items():
            expected = {target for target in intent.targets if target.kind in {"memory", "raw"}}
            if actual[operation] != expected:
                raise GovernanceError("governance_execution_tampered")
        return result

    def suppressed(self, scope_id):
        """record -> kind, or "purged" when any purge covers it; conservative on crash."""
        purges = {key for key, (_, intent) in self.operations().items() if intent.action == "purge"}
        result = {}
        for row in self._rows("SELECT * FROM suppressed WHERE scope_id=?", (scope_id,)):
            if row["operation_id"] in purges:
                result[row["record_id"]] = "purged"
            else:
                result.setdefault(row["record_id"], row["kind"])
        return result

    def records(self, operation_id):
        return sorted(
            row["record_id"]
            for row in self._rows("SELECT * FROM suppressed WHERE operation_id=?", (operation_id,))
        )

    def plan(self, intent, records):
        payload = intent.model_dump_json()
        with self._connection(writing=True) as db:
            existing = db.execute(
                "SELECT * FROM operations WHERE operation_id=?", (intent.operation_id,)
            ).fetchone()
            if existing is not None:
                self._check(existing, "operations")
                if existing["intent"] != payload:
                    raise GovernanceError("governance_operation_conflict")
                return
            values = [intent.operation_id, intent.scope_id, payload, "planned"]
            db.execute(
                "INSERT INTO operations VALUES (?,?,?,?,?)",
                (*values, self._sign(["operations", *values])),
            )
            for identity, kind in sorted(records.items()):
                values = [intent.operation_id, intent.scope_id, identity, kind]
                db.execute(
                    "INSERT INTO suppressed VALUES (?,?,?,?,?)",
                    (*values, self._sign(["suppressed", *values])),
                )

    def complete(self, intent):
        values = [intent.operation_id, intent.scope_id, intent.model_dump_json(), "completed"]
        with self._connection(writing=True) as db:
            changed = db.execute(
                "UPDATE operations SET state='completed', signature=? "
                "WHERE operation_id=? AND state='planned'",
                (self._sign(["operations", *values]), intent.operation_id),
            ).rowcount
            if (
                not changed
                and not db.execute(
                    "SELECT 1 FROM operations WHERE operation_id=? AND state='completed'",
                    (intent.operation_id,),
                ).fetchone()
            ):
                raise GovernanceError("governance_operation_unplanned")

    def discard(self, operation_id):
        """Only for a plan whose intent never reached the journal."""
        with self._connection(writing=True) as db:
            db.execute("DELETE FROM suppressed WHERE operation_id=?", (operation_id,))
            db.execute(
                "DELETE FROM operations WHERE operation_id=? AND state='planned'", (operation_id,)
            )


def pending_operations(journal, store):
    """Journal intents the execution store has not finished."""
    done = {key: state for key, (state, _) in store.operations().items()}
    pending = []
    for scope in journal.scopes():
        cursor = None
        while True:
            page = journal.changes(scope, after=cursor, limit=1000)
            for entry in page["entries"]:
                intent = Intent.model_validate(entry["intent"])
                if done.get(intent.operation_id) != "completed":
                    pending.append(intent)
            cursor = page["cursor"]
            if not page["has_more"]:
                break
    return pending


class GovernanceExecutor:
    def __init__(
        self, authority, bridge, previews, admission, lineage=None, purger=None, drain=None
    ):
        self.purger = purger
        self.drain = drain
        self.ready = False
        self._replayed = False
        self._resume_lock = asyncio.Lock()
        self.authority = authority
        self.bridge = bridge
        self.previews = previews
        self.admission = admission
        self.lineage = lineage
        self.store = ExecutionStore(authority.journal)
        # One administrator identity today; recorded so later multi-admin audit
        # does not need a journal migration.
        self.actor_id = authority.access.instance_id

    @asynccontextmanager
    async def _exclusive(self):
        # Workers need shared SDK admission through output observation. Drain
        # their whole lifetime before closing SDK and intake admission.
        async with self.drain().quiesce() if self.drain is not None else nullcontext():
            async with self.admission.exclusive():
                yield

    async def run(self, request):
        try:
            request = ExecuteRequest.model_validate(request)
        except ValidationError:
            raise AccessError("invalid_request", 422) from None
        try:
            return await self.execute(request, self.actor_id)
        except GovernanceError as error:
            code = str(error)
            status = {
                "governance_selection_not_found": 404,
                "governance_preview_changed": 409,
                "governance_operation_conflict": 409,
                "governance_operation_request_unproven": 409,
                "governance_reference_unresolved": 409,
                "governance_scan_limit": 409,
            }.get(code, 503)
            raise AccessError(code, status) from None
        except AccessError:
            raise
        except Exception:
            # The SDK may raise with stored content in the message.
            raise AccessError("governance_execution_unavailable", 503) from None

    async def _suppress_sdk(self, sdk, identities, *, scope_id):
        """archived/is_latest only: excluded from SDK recall and reconcile, origin intact."""
        vector = sdk._vector_store
        namespace = f"cmh:{self.authority.access.instance_id}:{scope_id}"

        async def run():
            for identity in identities:
                node = await vector.get_by_id(identity)
                if node is None:
                    # The SDK also returns None on read/decode errors. Prove
                    # actual absence before treating a target as already gone.
                    found = await _run_in_vdb_pool(
                        vector._collection.get,
                        ids=[vector._node_id_to_point_id(identity)],
                        include=[],
                    )
                    if not isinstance(found, dict) or found.get("ids") != []:
                        raise GovernanceError("governance_suppression_incomplete")
                    continue
                if node.node_id != identity:
                    raise GovernanceError("governance_record_identity_mismatch")
                if node.user_id != namespace:
                    raise GovernanceError("governance_scope_mismatch")
                if node.status.value != SUPPRESSED_STATUS or node.is_latest:
                    if not await vector.update_payload(
                        identity, {"status": SUPPRESSED_STATUS, "is_latest": False}
                    ):
                        raise GovernanceError("governance_suppression_incomplete")

        try:
            await _joined_thread(lambda: sdk._loop_thread.run(run()))
        except GovernanceError:
            raise
        except Exception:
            raise GovernanceError("governance_suppression_incomplete") from None

    async def _finish(self, intent, records, contents=()):
        if intent.action == "forget":
            require_forget_supported(records)
        await require_modeled_scope(
            self.bridge(), f"cmh:{self.authority.access.instance_id}:{intent.scope_id}"
        )
        native = [i for i in records if not i.startswith(RESERVED_PREFIXES)]
        if intent.action == "purge":
            # Suppression rows stay: they are the durable rule that blocks
            # re-extraction and re-import after the content itself is gone.
            # contents is only available in the executing process; a resumed
            # purge is verified by ID absence through the same idempotent steps.
            await self.purger.run(records, contents, scope_id=intent.scope_id)
        else:
            # Hub-owned identities (originals, input snapshots) have no SDK node;
            # the Hub suppression table alone governs them.
            await self._suppress_sdk(
                await self.bridge()._get_client(), native, scope_id=intent.scope_id
            )
        await _joined_thread(self.store.complete, intent)

    async def replay_purges(self):
        """Reapply completed forgets and purges after business restores (ADR-0032).

        Runs once per process before writes. Reuse the original idempotent
        effects without appending intents or rebuilding partly deleted lineage.
        """
        if self._replayed:
            return
        async with self._exclusive():
            operations = await _joined_thread(self.store.operations)
            # Also check completed forgets and entity-only restores before
            # deciding whether any modeled purge target is still present.
            for scope_id in sorted({intent.scope_id for _, intent in operations.values()}):
                await require_modeled_scope(
                    self.bridge(), f"cmh:{self.authority.access.instance_id}:{scope_id}"
                )
            # Refuse committed unsupported scopes before any recovery mutation.
            # An uncommitted plan is disposable and must reach _resume's discard.
            committed = set()
            if any(state == "planned" for state, _ in operations.values()):
                committed = {
                    intent.operation_id
                    for intent in await _joined_thread(
                        pending_operations, self.authority.journal, self.store
                    )
                }
            for operation_id, (state, intent) in operations.items():
                if intent.action == "forget" and (
                    state == "completed" or operation_id in committed
                ):
                    require_forget_supported(await _joined_thread(self.store.records, operation_id))
            for operation_id, (state, intent) in operations.items():
                if state != "completed":
                    continue
                records = await _joined_thread(self.store.records, operation_id)
                if intent.action == "forget":
                    native = [i for i in records if not i.startswith(RESERVED_PREFIXES)]
                    await self._suppress_sdk(
                        await self.bridge()._get_client(), native, scope_id=intent.scope_id
                    )
                elif self.purger is not None:
                    if await self.purger.present(records, scope_id=intent.scope_id):
                        await self.purger.run(records, (), scope_id=intent.scope_id)
            if self.purger is not None:
                self._replayed = True

    async def resume(self):
        """Finish planned forgets, drop plans whose intent never committed."""
        # Core starts its maintenance loop inside its lifespan. That loop can
        # reach resume before the Hub's startup resume has completed.
        async with self._resume_lock:
            await self._resume()
            self.ready = True

    async def _resume(self):
        await self.replay_purges()
        planned = await _joined_thread(self.store.operations)
        if all(state == "completed" for state, _ in planned.values()):
            return
        async with self._exclusive():
            pending = {
                intent.operation_id
                for intent in await _joined_thread(
                    pending_operations, self.authority.journal, self.store
                )
            }
            for operation_id, (state, intent) in self.store.operations().items():
                if state == "completed":
                    continue
                if operation_id not in pending:
                    await _joined_thread(self.store.discard, operation_id)
                    continue
                await self._finish(intent, self.store.records(operation_id))

    async def execute(self, request: ExecuteRequest, actor_id: str) -> dict:
        request = ExecuteRequest.model_validate(request)
        async with self._resume_lock:
            return await self._execute(request, actor_id)

    async def _execute(self, request: ExecuteRequest, actor_id: str) -> dict:
        if request.operation_id is not None:
            # A retried request after a lost response: the view has since changed
            # (records are archived), so answer from the durable record instead.
            known = (await _joined_thread(self.store.operations)).get(request.operation_id)
            if known is not None:
                state, intent = known
                require_same_request(self.authority.journal, intent, request, actor_id)
                if state != "completed":
                    # The governance lock is already held. Resume may discard
                    # an orphan plan, so only its fresh durable state is truth.
                    await self._resume()
                    known = (await _joined_thread(self.store.operations)).get(request.operation_id)
                if known is not None:
                    state, intent = known
                    require_same_request(self.authority.journal, intent, request, actor_id)
                    if state != "completed":
                        raise GovernanceError("governance_execution_pending")
                    records = await _joined_thread(self.store.records, intent.operation_id)
                    if intent.action == "forget":
                        require_forget_supported(records)
                    return {
                        "operation_id": intent.operation_id,
                        "scope_id": intent.scope_id,
                        "action": intent.action,
                        "state": "completed",
                        "suppressed_count": len(records),
                    }
                # No committed intent existed: revalidate the confirmed view
                # and really execute, rather than reporting the discarded plan.
        namespace = f"cmh:{self.authority.access.instance_id}:{request.scope_id}"
        bridge = self.bridge()
        # Pause claims and drain whole workers before recomputing the scope.
        async with self._exclusive():
            await _joined_thread(self.previews._require_scope, request.scope_id)
            nodes = await observe_inventory(bridge, namespace, self.lineage)
            preview = await _joined_thread(
                lambda: self.previews._format(
                    PreviewRequest(
                        scope_id=request.scope_id,
                        action=request.action,
                        record_ids=request.record_ids,
                        limit=1,
                    ),
                    nodes,
                )
            )
            if preview["view_digest"] != request.expected_view:
                raise GovernanceError("governance_preview_changed")
            plan = plan_relations(nodes, request.record_ids)
            records = {identity: nodes[identity].kind for identity in plan["record_ids"]}
            if request.action == "forget":
                require_forget_supported(records)
            if request.action == "purge":
                from cloud_memory_hub.governance_purge import partition

                partition(records)  # Refuse unsupported kinds before any intent.
            contents = [nodes[identity].content for identity in plan["record_ids"]]
            journal = self.authority.journal
            sources = await _joined_thread(
                capture_sources, journal, self.lineage.ingest, request.scope_id, records
            )
            rules = sources + request_targets(journal, request, actor_id)
            if len(records) + len(rules) > MAX_TARGETS:
                raise GovernanceError("governance_scan_limit")
            intent = Intent(
                operation_id=request.operation_id or str(uuid4()),
                actor_id=actor_id,
                scope_id=request.scope_id,
                action=request.action,
                targets=tuple(
                    journal.target(request.scope_id, kind, identity)
                    for identity, kind in sorted(records.items())
                )
                + rules,
            )
            # Order: plan (IDs) -> journal intent -> SDK state -> completed.
            await _joined_thread(self.store.plan, intent, records)
            checkpoint = await _joined_thread(self.authority.append, intent)
            await self._finish(intent, records, contents)
        purge = request.action == "purge"
        return {
            "operation_id": intent.operation_id,
            "scope_id": request.scope_id,
            "action": request.action,
            "state": "completed",
            "checkpoint": json.loads(checkpoint.model_dump_json()),
            "suppressed_count": len(records),
            "raw_retained_for_explicit_read": 0
            if purge
            else sum(kind == "raw" for kind in records.values()),
        }
