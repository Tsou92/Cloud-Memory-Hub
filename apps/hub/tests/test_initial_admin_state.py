"""FR-10/12: initial access requires the installer's capability and durable rotation."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from cloud_memory_hub.__main__ import initialize
from cloud_memory_hub.admin_credentials import (
    ADMIN_SETUP_PREFIX,
    AdminCredentialError,
    AdminCredentials,
    initial_admin_setup_proof,
    new_initial_admin_token,
)
from cloud_memory_hub.config import HubConfig, read_private_file

NEW = "synthetic-new-" + "b" * 40
OTHER = "synthetic-next-" + "c" * 40


@pytest.fixture
def fresh_configuration(tmp_path):
    directory = tmp_path / "config"
    governance = tmp_path / "governance"
    initialize(directory, governance_dir=governance)

    def reload_config():
        return HubConfig(
            tmp_path / "data",
            directory,
            "https://memory.example.test",
            read_private_file(directory / "admin-token").decode("ascii").strip(),
            read_private_file(directory / "encryption-key").strip(),
            governance_dir=governance,
        )

    return reload_config


def test_initializer_persists_setup_state_without_storing_admin_as_bearer(fresh_configuration):
    config = fresh_configuration()
    assert config.admin_token.startswith(ADMIN_SETUP_PREFIX)
    proof = initial_admin_setup_proof(config.admin_token)
    assert proof and len(proof) == 64 and proof not in config.admin_token
    assert initial_admin_setup_proof(fresh_configuration().admin_token) == proof
    assert initial_admin_setup_proof(new_initial_admin_token()) != proof
    store = AdminCredentials(config)
    assert store.setup_pending and store.valid(config.admin_token)
    assert not store.valid("admin") and not store.valid(proof)
    assert proof in store.known_credentials()


def test_setup_validates_both_inputs_before_durable_rotation(fresh_configuration):
    original = fresh_configuration()
    store = AdminCredentials(original)
    proof = initial_admin_setup_proof(original.admin_token)
    for password, supplied in (("admin", "0" * 64), ("incorrect", proof), ("admin", "非ASCII")):
        with pytest.raises(AdminCredentialError, match="invalid_initial_credential"):
            store.complete_initial_setup(password, supplied, NEW)
    for rejected in ("admin", new_initial_admin_token()):
        with pytest.raises(AdminCredentialError, match="invalid_new_credential"):
            store.complete_initial_setup("admin", proof, rejected)
    assert fresh_configuration().admin_token == original.admin_token
    assert store.setup_pending

    store.complete_initial_setup("admin", proof, NEW)
    assert not store.setup_pending and store.valid(NEW)
    assert not store.valid(original.admin_token) and not store.valid("admin")
    assert proof in store.known_credentials()
    persisted = fresh_configuration()
    assert persisted.admin_token == NEW
    assert persisted.encryption_key == original.encryption_key
    assert initial_admin_setup_proof(persisted.admin_token) is None
    reopened = AdminCredentials(persisted)
    with pytest.raises(AdminCredentialError, match="initial_setup_not_pending"):
        reopened.complete_initial_setup("admin", proof, OTHER)
    assert reopened.valid(NEW)
    with pytest.raises(AdminCredentialError, match="invalid_new_credential"):
        reopened.change(NEW, new_initial_admin_token())


def test_concurrent_initial_setup_consumes_proof_once(fresh_configuration):
    config = fresh_configuration()
    store = AdminCredentials(config)
    proof = initial_admin_setup_proof(config.admin_token)
    ready = Barrier(2)

    def attempt(replacement):
        ready.wait(timeout=3)
        try:
            store.complete_initial_setup("admin", proof, replacement)
        except AdminCredentialError as error:
            return error.code
        return "completed"

    with ThreadPoolExecutor(max_workers=2) as workers:
        assert sorted(workers.map(attempt, (NEW, OTHER))) == [
            "completed",
            "initial_setup_not_pending",
        ]
    assert not store.setup_pending
    persisted = fresh_configuration().admin_token
    assert persisted in (NEW, OTHER) and store.valid(persisted)
