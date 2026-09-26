"""Device credential storage (spec §6.10).

Device passwords are encrypted at rest inside ``devices.config``, in the
fields a schema marks ``encrypted``, with AES-256-GCM. The key is the 32
random bytes in ``/srv/appliance/device-secret`` (mode 0400), generated at
first boot and deliberately excluded from the backup archive: restoring
``/data`` to a replacement machine means re-entering device passwords, and
that case surfaces as :class:`SecretMismatch`, never as garbage.

An encrypted value is stored as ``{"enc": "<base64 nonce+ciphertext>"}`` so it
can never be mistaken for a plain string. The field key is bound in as
associated data, so a ciphertext moved to another field does not decrypt.
"""

from __future__ import annotations

import base64
import os
import secrets as _secrets
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from proskenion.core.drivers.fields import ENCRYPTED_KEY, Field, is_encrypted_value

DEFAULT_SECRET_PATH = Path("/srv/appliance/device-secret")
SECRET_LENGTH = 32  # AES-256
NONCE_LENGTH = 12  # GCM standard


class SecretMismatch(Exception):
    """The stored value was not encrypted with this machine's secret.

    The expected cause is a restore to new hardware (§6.10); the fix is to
    re-enter the device password.
    """


class SecretUnavailable(Exception):
    """The secret file is missing, unreadable or the wrong size."""


def generate_secret_if_missing(path: Path | str = DEFAULT_SECRET_PATH) -> Path:
    """Create the secret file with 32 random bytes and mode 0400 unless it exists."""
    path = Path(path)
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    # O_BINARY: Windows would otherwise expand a newline byte to CR LF.
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags, 0o400)
    try:
        os.write(fd, _secrets.token_bytes(SECRET_LENGTH))
    finally:
        os.close(fd)
    os.chmod(path, 0o400)  # O_CREAT mode is masked by umask; make it exact
    return path


class DeviceSecret:
    """AES-256-GCM over the fields a schema marks ``encrypted``."""

    def __init__(self, key: bytes) -> None:
        if len(key) != SECRET_LENGTH:
            raise SecretUnavailable(f"device secret must be {SECRET_LENGTH} bytes")
        self._aead = AESGCM(key)

    @classmethod
    def load(cls, path: Path | str = DEFAULT_SECRET_PATH) -> DeviceSecret:
        try:
            key = Path(path).read_bytes()
        except OSError as exc:
            raise SecretUnavailable(f"cannot read device secret {path}: {exc}") from exc
        return cls(key)

    # -- single values ------------------------------------------------------

    def encrypt_value(self, field_key: str, plain: str) -> dict[str, str]:
        nonce = _secrets.token_bytes(NONCE_LENGTH)
        ciphertext = self._aead.encrypt(nonce, plain.encode("utf-8"), field_key.encode("utf-8"))
        return {ENCRYPTED_KEY: base64.b64encode(nonce + ciphertext).decode("ascii")}

    def decrypt_value(self, field_key: str, stored: Mapping[str, Any]) -> str:
        if not is_encrypted_value(stored):
            raise SecretMismatch(f"field {field_key!r} is not in the encrypted form")
        try:
            blob = base64.b64decode(stored[ENCRYPTED_KEY], validate=True)
        except (ValueError, TypeError) as exc:
            raise SecretMismatch(f"field {field_key!r}: stored value is not valid") from exc
        if len(blob) <= NONCE_LENGTH:
            raise SecretMismatch(f"field {field_key!r}: stored value is too short")
        nonce, ciphertext = blob[:NONCE_LENGTH], blob[NONCE_LENGTH:]
        try:
            plain = self._aead.decrypt(nonce, ciphertext, field_key.encode("utf-8"))
        except InvalidTag as exc:
            raise SecretMismatch(
                f"field {field_key!r} was encrypted with a different device secret; "
                "re-enter the password"
            ) from exc
        return plain.decode("utf-8")

    # -- whole configuration blocks -----------------------------------------

    def encrypt_config(self, schema: Sequence[Field], values: Mapping[str, Any]) -> dict[str, Any]:
        """Return ``values`` with each ``encrypted`` field's plain string
        replaced by its encrypted form. Fields not marked are untouched; a
        value already encrypted is kept as it is (an unchanged password on
        re-save)."""
        result = dict(values)
        for field in schema:
            if not field.encrypted or field.key not in result:
                continue
            value = result[field.key]
            if value is None or is_encrypted_value(value):
                continue
            if not isinstance(value, str):
                raise TypeError(f"field {field.key!r}: only text can be encrypted")
            result[field.key] = self.encrypt_value(field.key, value)
        return result

    def decrypt_config(self, schema: Sequence[Field], values: Mapping[str, Any]) -> dict[str, Any]:
        """Return ``values`` with each ``encrypted`` field's stored form
        replaced by the plain string. Raises :class:`SecretMismatch` if any
        value was encrypted under a different secret."""
        result = dict(values)
        for field in schema:
            if not field.encrypted or field.key not in result:
                continue
            value = result[field.key]
            if value is None or not is_encrypted_value(value):
                continue
            result[field.key] = self.decrypt_value(field.key, value)
        return result
