import os

import pytest
from cloud_memory_hub.__main__ import initialize
from cloud_memory_hub.config import HubConfig, read_private_file
from cloud_memory_hub.secret_store import EncryptedSecretStore
from cryptography.fernet import Fernet
from one_memory.secrets import SecretStoreUnavailableError


def test_credentials_survive_restart_without_plaintext_storage(tmp_path):
    key = Fernet.generate_key()
    path = tmp_path / "credentials.sqlite3"
    synthetic_value = "example-model-value-" + "x" * 24
    store = EncryptedSecretStore(path, key)
    store.put("model-one", synthetic_value)
    assert synthetic_value.encode() not in path.read_bytes()
    assert path.stat().st_mode & 0o777 == 0o600
    reopened = EncryptedSecretStore(path, key)
    assert reopened.resolve("model-one") == synthetic_value
    assert reopened.resolve("absent") is None
    assert reopened.has("model-one")
    with pytest.raises(SecretStoreUnavailableError):
        EncryptedSecretStore(path, Fernet.generate_key())
    assert reopened.resolve("model-one") == synthetic_value
    reopened.delete_many(("model-one", "absent"))
    assert not reopened.has("model-one")


def test_initializer_never_rotates_existing_credentials(tmp_path):
    directory = tmp_path / "config"
    initialize(directory)
    before = {
        name: read_private_file(directory / name) for name in ["admin-token", "encryption-key"]
    }
    with pytest.raises(ValueError, match="already exists"):
        initialize(directory)
    assert before == {name: read_private_file(directory / name) for name in before}


def test_private_files_reject_readable_permissions_and_symlinks(tmp_path):
    path = tmp_path / "credential"
    path.write_bytes(b"synthetic")
    os.chmod(path, 0o644)
    with pytest.raises(ValueError, match="private"):
        read_private_file(path)
    os.chmod(path, 0o600)
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(OSError):
        read_private_file(link)
    with pytest.raises(OSError):
        EncryptedSecretStore(link, Fernet.generate_key())


@pytest.mark.parametrize(
    "origin",
    [
        "http://nas.local:8800",
        "https://user:pass@nas.local",  # pragma: allowlist secret - synthetic rejected userinfo
        "https://nas.local/path",
    ],
)
def test_unsafe_public_origin_rejected(tmp_path, origin):
    with pytest.raises(ValueError):
        HubConfig(tmp_path, tmp_path, origin, "a" * 40, Fernet.generate_key())
