"""The device secret, root-side, decrypt only (§6.10).

``proskenion.core.secrets.DeviceSecret`` is the application's implementation;
this is the sibling that runs where the application's venv may not exist —
emergency mode, reading ``/srv/appliance/smtp-fallback.toml``'s encrypted
password with no database and no application running (§4.6,
``read_fallback``). Same file (``/srv/appliance/device-secret``), same
AES-256-GCM, same ``{"enc": "<base64 nonce+ciphertext+tag>"}`` shape, the
field key bound in as associated data so a ciphertext moved to another field
does not decrypt. The two sides are proved to agree in
``tests/unit/appliance/test_device_secret_agreement.py``, the discipline
``auditorium_bootstate.py`` already uses for ``boot-state.json``.

This needs the ``cryptography`` package, exactly as
``appliance/lib/packages.py`` (installed from ``proskenion/core/packages.py``
at image build, §6.11) already does — neither is standard library, and both
therefore need ``python3-cryptography`` on the read-only root's system Python
(``appliance/image/build.sh``'s apt line), not the application's venv on
``/data``, which this code must keep working without. The import is deferred
to :func:`decrypt_value` so a responder with no password to decrypt — the
common case: Q22's relay is unauthenticated — never pays for it, and its
absence degrades one alert to unauthenticated rather than refusing to serve
``/health`` at all.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

DEFAULT_SECRET_PATH = Path("/srv/appliance/device-secret")
SECRET_LENGTH = 32  # AES-256
NONCE_LENGTH = 12  # GCM standard
ENCRYPTED_KEY = "enc"


class SecretUnavailable(Exception):
    """The secret file, or the ``cryptography`` package, is not usable here."""


class SecretMismatch(Exception):
    """The stored value was not encrypted with this machine's secret, or is malformed."""


def load_key(path: Path | str = DEFAULT_SECRET_PATH) -> bytes:
    try:
        key = Path(path).read_bytes()
    except OSError as exc:
        raise SecretUnavailable(f"cannot read device secret {path}: {exc}") from exc
    if len(key) != SECRET_LENGTH:
        raise SecretUnavailable(f"device secret must be {SECRET_LENGTH} bytes")
    return key


def decrypt_value(key: bytes, field_key: str, stored: dict[str, Any]) -> str:
    """The plain string ``proskenion.core.secrets.DeviceSecret.encrypt_value``
    produced for ``field_key``.

    Raises :class:`SecretUnavailable` if ``cryptography`` is not importable
    here, and :class:`SecretMismatch` for anything that does not verify — a
    different device secret, a truncated value, a field key that does not
    match the one it was encrypted under.
    """
    try:
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:
        raise SecretUnavailable(
            "the cryptography package is not installed on the read-only root"
        ) from exc
    if not isinstance(stored, dict) or ENCRYPTED_KEY not in stored:
        raise SecretMismatch(f"field {field_key!r} is not in the encrypted form")
    try:
        blob = base64.b64decode(stored[ENCRYPTED_KEY], validate=True)
    except (ValueError, TypeError) as exc:
        raise SecretMismatch(f"field {field_key!r}: stored value is not valid") from exc
    if len(blob) <= NONCE_LENGTH:
        raise SecretMismatch(f"field {field_key!r}: stored value is too short")
    nonce, ciphertext = blob[:NONCE_LENGTH], blob[NONCE_LENGTH:]
    try:
        plain = AESGCM(key).decrypt(nonce, ciphertext, field_key.encode("utf-8"))
    except InvalidTag as exc:
        raise SecretMismatch(
            f"field {field_key!r} was encrypted with a different device secret; "
            "re-enter the password"
        ) from exc
    return plain.decode("utf-8")
