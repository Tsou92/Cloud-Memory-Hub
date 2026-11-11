"""Atomic administrator credential replacement without changing device identities."""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import stat
import tempfile
import threading

from cloud_memory_hub.config import HubConfig, read_private_file
from cloud_memory_hub.privacy import CredentialFilter, PrivacyError

ADMIN_SETUP_PREFIX = "cmh_admin_setup_v1_"
_SETUP_TOKEN = re.compile(ADMIN_SETUP_PREFIX + r"[A-Za-z0-9_-]{64}")
_SETUP_PROOF_DOMAIN = b"cloud-memory-hub/administrator/setup/v1"
_PASSWORD_RECORD_PREFIX = "cmh_admin_password_v1_"  # pragma: allowlist secret -- format prefix
_PASSWORD_RECORD = re.compile(_PASSWORD_RECORD_PREFIX + r"([0-9a-f]{32})_([0-9a-f]{64})")
_PASSWORD_ROUNDS = 600_000


def new_initial_admin_token() -> str:
    """Generate the private bearer and persist pending setup in its format."""
    return ADMIN_SETUP_PREFIX + secrets.token_urlsafe(48)


def initial_admin_setup_proof(token: str) -> str | None:
    """Derive the browser setup capability without revealing the private bearer."""
    if not _SETUP_TOKEN.fullmatch(token):
        return None
    return hmac.digest(token.encode("ascii"), _SETUP_PROOF_DOMAIN, "sha256").hex()


class AdminCredentialError(Exception):
    """Only a fixed public code and status; never include submitted credentials."""

    def __init__(self, code: str, status: int):
        self.code = code
        self.status = status
        super().__init__(code)


def _legacy_bearer_format(value: str) -> bool:
    return (
        32 <= len(value) <= 256
        and not value.startswith(ADMIN_SETUP_PREFIX)
        and not value.startswith(_PASSWORD_RECORD_PREFIX)
        and all(33 <= ord(character) <= 126 for character in value)
    )


def _password_digest(value: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac(
        "sha256", value.encode("utf-8", "surrogatepass"), salt, _PASSWORD_ROUNDS
    )


def _credential_record(password: str) -> str:
    if _legacy_bearer_format(password):
        return password
    salt = secrets.token_bytes(16)
    return f"{_PASSWORD_RECORD_PREFIX}{salt.hex()}_{_password_digest(password, salt).hex()}"


class AdminCredentials:
    """One serving process owns the config volume under the existing storage lease.

    The small synchronous critical section serializes reauthentication, persistence
    and the shared privacy-filter source. It never yields between those steps.
    """

    def __init__(self, config: HubConfig):
        self._directory = config.config_dir
        self._token = config.admin_token
        self._known = (config.admin_token, config.encryption_key.decode())
        if (proof := initial_admin_setup_proof(self._token)) is not None:
            self._known = (*self._known, proof)
        self._available = True
        self._lock = threading.RLock()

    @property
    def available(self) -> bool:
        return self._available

    @property
    def setup_pending(self) -> bool:
        with self._lock:
            return _SETUP_TOKEN.fullmatch(self._token) is not None

    def valid(self, value: object) -> bool:
        with self._lock:
            return (
                self._available
                and isinstance(value, str)
                and value.isascii()
                and hmac.compare_digest(value, self._token)
            )

    def valid_password(self, value: object) -> bool:
        with self._lock:
            if self.valid(value):
                return True
            if not self._available or not isinstance(value, str) or not value:
                return False
            record = _PASSWORD_RECORD.fullmatch(self._token)
            if record is None:
                return False
            salt, expected = record.groups()
            return hmac.compare_digest(
                _password_digest(value, bytes.fromhex(salt)), bytes.fromhex(expected)
            )

    def known_credentials(self) -> tuple[str, ...]:
        with self._lock:
            if not self._available:
                raise PrivacyError("known_credentials_unavailable")
            return self._known

    def complete_initial_setup(self, password: object, proof: object, replacement: object) -> None:
        with self._lock:
            if not self._available:
                raise AdminCredentialError("admin_credential_unavailable", 503)
            expected = initial_admin_setup_proof(self._token)
            if expected is None:
                raise AdminCredentialError("initial_setup_not_pending", 409)
            password_matches = (
                isinstance(password, str)
                and password.isascii()
                and hmac.compare_digest(password, "admin")
            )
            proof_matches = (
                isinstance(proof, str) and proof.isascii() and hmac.compare_digest(proof, expected)
            )
            if not password_matches or not proof_matches:
                raise AdminCredentialError("invalid_initial_credential", 401)
            if replacement == password:
                raise AdminCredentialError("invalid_new_credential", 422)
            # The nested lock keeps verification and the durable replacement in one operation.
            self.change(self._token, replacement)

    def change(
        self,
        current: object,
        replacement: object,
    ) -> None:
        with self._lock:
            if not self._available:
                raise AdminCredentialError("admin_credential_unavailable", 503)
            if not self.valid_password(current):
                raise AdminCredentialError("current_credential_incorrect", 403)
            if not isinstance(replacement, str) or not replacement or replacement == current:
                raise AdminCredentialError("invalid_new_credential", 422)
            replacement = _credential_record(replacement)
            known = tuple(dict.fromkeys((*self._known, replacement)))
            try:
                # Console reads add one opaque browser session to this set.
                CredentialFilter((*known, "x" * 43))
            except PrivacyError:
                raise AdminCredentialError("credential_change_unavailable", 503) from None
            path = self._directory / "admin-token"
            temporary = None
            try:
                info = self._directory.lstat()
                if (
                    not stat.S_ISDIR(info.st_mode)
                    or info.st_uid != os.getuid()
                    or info.st_mode & 0o077
                ):
                    raise ValueError("unsafe_configuration_directory")
                existing = read_private_file(path).decode("ascii").strip()
                if not hmac.compare_digest(existing, self._token):
                    self._available = False
                    raise AdminCredentialError("admin_credential_unavailable", 503)
                descriptor, temporary = tempfile.mkstemp(
                    prefix=".admin-token-", dir=self._directory
                )
                with os.fdopen(descriptor, "wb") as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    stream.write(replacement.encode("ascii") + b"\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
                descriptor = os.open(self._directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            except AdminCredentialError:
                raise
            except Exception:
                # A failing replace/fsync can occur after publication. Determine
                # the actual authority rather than silently keeping a stale token.
                try:
                    actual = read_private_file(path).decode("ascii").strip()
                except Exception:
                    self._available = False
                    raise AdminCredentialError("admin_credential_unavailable", 503) from None
                if not hmac.compare_digest(actual, replacement):
                    if not hmac.compare_digest(actual, self._token):
                        self._available = False
                    raise AdminCredentialError("credential_change_unavailable", 503) from None
            finally:
                if temporary is not None:
                    try:
                        os.unlink(temporary)
                    except FileNotFoundError:
                        pass
                    except OSError:
                        # A leftover private file does not undo a known publication.
                        pass
            self._token = replacement
            self._known = known
