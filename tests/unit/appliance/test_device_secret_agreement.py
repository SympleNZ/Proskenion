"""``auditorium_device_secret.py`` has to decrypt what
``proskenion.core.secrets.DeviceSecret`` encrypts (§6.10) — the same
agreement discipline ``test_boot_state_agreement.py`` uses for
``boot-state.json``. This is emergency mode's one dependency past the
standard library (module docstring), so the two sides are proved to produce
and consume the exact same bytes rather than merely "look compatible".
"""

from __future__ import annotations

import os
from pathlib import Path
from types import ModuleType

import pytest

cryptography = pytest.importorskip("cryptography")

from proskenion.core.secrets import DeviceSecret  # noqa: E402


@pytest.fixture
def key() -> bytes:
    return os.urandom(32)


def test_the_root_side_decrypts_what_the_application_encrypts(
    key: bytes, device_secret: ModuleType
) -> None:
    app_side = DeviceSecret(key)
    encrypted = app_side.encrypt_value("smtp_fallback_password", "correct horse battery staple")

    plain = device_secret.decrypt_value(key, "smtp_fallback_password", encrypted)
    assert plain == "correct horse battery staple"


def test_a_different_secret_is_a_mismatch_not_a_crash(
    key: bytes, device_secret: ModuleType
) -> None:
    app_side = DeviceSecret(key)
    encrypted = app_side.encrypt_value("smtp_fallback_password", "hunter2")

    other_key = os.urandom(32)
    with pytest.raises(device_secret.SecretMismatch):
        device_secret.decrypt_value(other_key, "smtp_fallback_password", encrypted)


def test_the_field_key_is_bound_in_as_associated_data(
    key: bytes, device_secret: ModuleType
) -> None:
    """A ciphertext moved to a different field must not decrypt (§6.10)."""
    app_side = DeviceSecret(key)
    encrypted = app_side.encrypt_value("smtp_fallback_password", "hunter2")

    with pytest.raises(device_secret.SecretMismatch):
        device_secret.decrypt_value(key, "some_other_field", encrypted)


def test_a_malformed_value_is_a_mismatch(key: bytes, device_secret: ModuleType) -> None:
    with pytest.raises(device_secret.SecretMismatch):
        device_secret.decrypt_value(key, "smtp_fallback_password", {"enc": "not base64!!"})
    with pytest.raises(device_secret.SecretMismatch):
        device_secret.decrypt_value(key, "smtp_fallback_password", {"not_enc": "x"})


def test_load_key_reads_the_secret_file(tmp_path: Path, device_secret: ModuleType) -> None:
    path = tmp_path / "device-secret"
    path.write_bytes(os.urandom(32))
    key = device_secret.load_key(path)
    assert len(key) == 32


def test_load_key_refuses_the_wrong_length(tmp_path: Path, device_secret: ModuleType) -> None:
    path = tmp_path / "device-secret"
    path.write_bytes(b"too-short")
    with pytest.raises(device_secret.SecretUnavailable):
        device_secret.load_key(path)


def test_load_key_refuses_a_missing_file(tmp_path: Path, device_secret: ModuleType) -> None:
    with pytest.raises(device_secret.SecretUnavailable):
        device_secret.load_key(tmp_path / "does-not-exist")
