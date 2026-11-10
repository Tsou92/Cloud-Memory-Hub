"""ADR-0065 / FR-02, FR-03: durable, single-use and recoverable enrollment."""

import json
import secrets
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from cloud_memory_hub.remote_access import (
    AccessError,
    DeviceAccessStore,
    GrantRequest,
    NamedResource,
    RevisionRequest,
)
from cloud_memory_hub.remote_ingest import RemoteIngest
from cloud_memory_hub.remote_pairing import (
    PAIRING_PERMISSIONS,
    PairingClaim,
    PairingRequest,
    PairingStore,
)
from pydantic import ValidationError

ORIGIN = "https://memory.example.test"
NOW = datetime(2030, 1, 2, tzinfo=UTC)


@pytest.fixture
def pairings(tmp_path):
    access = DeviceAccessStore(tmp_path / "config" / "remote-access.sqlite3")
    return PairingStore(RemoteIngest(access, lambda: None), ORIGIN)


def invite(pairings, *, now=NOW):
    project = pairings.access.create_project(NamedResource(display_name="Synthetic project"))
    issued = pairings.create(
        PairingRequest(scope_id=project["project_id"], display_name="Synthetic installation"),
        now=now,
    )
    request = PairingClaim(
        pairing_id=issued.pairing["pairing_id"],
        invitation_secret=issued.invitation_secret.get_secret_value(),
        claimant_secret=secrets.token_urlsafe(32),
    )
    return issued, request


def credential(request, result):
    return f"cmhdev_v1_{result['device_id']}_{request.claimant_secret.get_secret_value()}"


def test_claim_persists_only_digests_and_reuses_one_identity_after_lost_response(pairings):
    issued, request = invite(pairings)
    assert pairings.access.list_devices() == []
    assert pairings.ingest.list_sources() == []
    assert pairings.get(request.pairing_id, now=NOW)["state"] == "pending"
    first = pairings.claim(request, now=NOW)
    token = credential(request, first)
    assert set(first) == {
        "schema_version",
        "kind",
        "origin",
        "instance_id",
        "device_id",
        "scope_id",
        "source_id",
    }
    assert first["origin"] == ORIGIN
    assert pairings.access.authenticate(token)["grants"] == [
        {"scope_id": first["scope_id"], "permissions": sorted(PAIRING_PERMISSIONS)}
    ]
    assert pairings.ingest.source_access(token, first["source_id"])["scope_id"] == first["scope_id"]
    restarted = PairingStore(
        RemoteIngest(DeviceAccessStore(pairings.access.path), lambda: None), ORIGIN
    )
    # The persisted claimant can recover a submitted result even after invitation expiry.
    assert restarted.claim(request, now=NOW + timedelta(days=1)) == first
    assert len(restarted.access.list_devices()) == len(restarted.ingest.list_sources()) == 1
    assert restarted.get(request.pairing_id, now=NOW)["state"] == "claimed"
    database = pairings.access.path.read_bytes()
    for secret in (
        issued.invitation_secret.get_secret_value(),
        request.claimant_secret.get_secret_value(),
        token,
    ):
        assert secret.encode() not in database
        assert secret not in repr(request)
        assert secret not in repr(issued)
        assert secret not in json.dumps(first)


def test_expired_cancelled_and_unknown_invites_cannot_create_devices(pairings):
    _, request = invite(pairings)
    with pytest.raises(AccessError, match="pairing_invalid_or_expired"):
        pairings.claim(request, now=NOW + timedelta(minutes=10))
    assert pairings.get(request.pairing_id, now=NOW + timedelta(minutes=10))["state"] == "expired"
    assert pairings.revoke(request.pairing_id, now=NOW)["state"] == "cancelled"
    assert pairings.revoke(request.pairing_id, now=NOW)["state"] == "cancelled"
    with pytest.raises(AccessError, match="pairing_invalid_or_expired"):
        pairings.claim(request, now=NOW)
    unknown = request.model_copy(update={"pairing_id": str(uuid4())})
    with pytest.raises(AccessError, match="pairing_invalid_or_expired"):
        pairings.claim(unknown, now=NOW)
    assert pairings.access.list_devices() == pairings.ingest.list_sources() == []


@pytest.mark.parametrize("same_claimant", [False, True])
def test_concurrent_claims_create_exactly_one_device_and_source(pairings, same_claimant):
    _, first = invite(pairings)
    second = (
        first
        if same_claimant
        else PairingClaim(
            pairing_id=first.pairing_id,
            invitation_secret=first.invitation_secret.get_secret_value(),
            claimant_secret=secrets.token_urlsafe(32),
        )
    )

    def claim(request):
        try:
            return pairings.claim(request, now=NOW)
        except AccessError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, [first, second]))
    successes = [item for item in results if isinstance(item, dict)]
    assert len(successes) == (2 if same_claimant else 1)
    if same_claimant:
        assert results[0] == results[1]
    else:
        assert "pairing_already_claimed" in results
    assert len(pairings.access.list_devices()) == len(pairings.ingest.list_sources()) == 1


def test_mid_transaction_failure_rolls_back_then_original_claim_can_retry(pairings, monkeypatch):
    _, request = invite(pairings)
    create_source = pairings.ingest._create_source

    def fail_after_source(db, registration):
        create_source(db, registration)
        raise sqlite3.OperationalError("synthetic interruption")

    monkeypatch.setattr(pairings.ingest, "_create_source", fail_after_source)
    with pytest.raises(AccessError, match="access_store_unavailable"):
        pairings.claim(request, now=NOW)
    assert pairings.access.list_devices() == pairings.ingest.list_sources() == []
    assert pairings.get(request.pairing_id, now=NOW)["state"] == "pending"
    monkeypatch.setattr(pairings.ingest, "_create_source", create_source)
    assert pairings.claim(request, now=NOW)["source_id"]


@pytest.mark.parametrize("change", ["rotate", "revoke", "source", "grant", "invitation"])
def test_recovery_never_reissues_revoked_or_changed_authority(pairings, change):
    _, request = invite(pairings)
    first = pairings.claim(request, now=NOW)
    device_id = first["device_id"]
    if change in {"rotate", "revoke"}:
        getattr(pairings.access, change)(device_id, RevisionRequest(expected_revision=2))
    elif change == "source":
        pairings.ingest.revoke_source(first["source_id"], RevisionRequest(expected_revision=1))
    elif change == "grant":
        pairings.access.replace_grants(device_id, GrantRequest(expected_revision=2, grants=[]))
    else:
        pairings.revoke(request.pairing_id, now=NOW)
    before = pairings.access.list_devices(), pairings.ingest.list_sources()
    code = "pairing_invalid_or_expired" if change == "invitation" else "pairing_result_unavailable"
    with pytest.raises(AccessError, match=code):
        pairings.claim(request, now=NOW)
    assert (pairings.access.list_devices(), pairings.ingest.list_sources()) == before


def test_bad_invitation_does_not_disclose_claimed_state(pairings):
    _, request = invite(pairings)
    pairings.claim(request, now=NOW)
    wrong = PairingClaim(
        pairing_id=request.pairing_id,
        invitation_secret=secrets.token_urlsafe(32),
        claimant_secret=request.claimant_secret.get_secret_value(),
    )
    with pytest.raises(AccessError, match="pairing_invalid_or_expired"):
        pairings.claim(wrong, now=NOW)


@pytest.mark.parametrize("scope", ["global", "*", "bad", 1])
def test_invites_require_explicit_project(scope):
    with pytest.raises(ValidationError):
        PairingRequest(scope_id=scope, display_name="Synthetic installation")


def test_missing_project_has_no_partial_invitation(pairings):
    with pytest.raises(AccessError, match="unknown_scope"):
        pairings.create(PairingRequest(scope_id=str(uuid4()), display_name="Synthetic"), now=NOW)
    with pairings.access._connection() as db:
        assert db.execute("SELECT count(*) FROM cmh_pairings").fetchone()[0] == 0


@pytest.mark.parametrize("secret", ["a", "a" * 44, "!" * 43, "_" * 43, b"A" * 43, None])
def test_claim_secrets_require_canonical_32_byte_encoding(secret):
    with pytest.raises(ValidationError):
        PairingClaim(
            pairing_id=str(uuid4()),
            invitation_secret=secret,
            claimant_secret=secrets.token_urlsafe(32),
        )


@pytest.mark.parametrize(
    "origin",
    [
        "",
        "http://memory.test",
        "https://memory.test/path",
        "https://a.test?",
        "https://synthetic-user@a.test",
        "https://a.test:99999",
    ],
)
def test_origin_is_explicit_and_uses_client_validation(pairings, origin):
    with pytest.raises(AccessError, match="pairing_origin_invalid"):
        PairingStore(pairings.ingest, origin)


def test_extension_is_additive_and_partial_or_newer_schema_fails_closed(pairings):
    with pairings.access._connection(writing=True) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        db.execute("UPDATE cmh_pairing_schema SET version=99")
    with pytest.raises(AccessError, match="pairing_store_version_unsupported"):
        PairingStore(pairings.ingest, ORIGIN)
    with pairings.access._connection(writing=True) as db:
        db.execute("DROP TABLE cmh_pairing_schema")
    with pytest.raises(AccessError, match="pairing_store_unrecognized"):
        PairingStore(pairings.ingest, ORIGIN)
