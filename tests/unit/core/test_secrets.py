"""Device credential storage (§6.10)."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from proskenion.core.drivers.fields import Field, is_encrypted_value
from proskenion.core.secrets import (
    SECRET_LENGTH,
    DeviceSecret,
    SecretMismatch,
    SecretUnavailable,
    generate_secret_if_missing,
)

SCHEMA = [
    Field("password", type="password", label="Password", encrypted=True),
    Field("note", type="string", label="Note"),
    Field("pin", type="password", label="PIN"),  # a password field not marked encrypted
]


def test_generate_secret_creates_32_random_bytes_read_only(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "device-secret"
    assert generate_secret_if_missing(path) == path
    data = path.read_bytes()
    assert len(data) == SECRET_LENGTH
    if sys.platform != "win32":
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o400
    # A second call leaves the existing secret alone.
    generate_secret_if_missing(path)
    assert path.read_bytes() == data


def test_generate_secret_writes_bytes_verbatim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A newline byte must not be translated to CR LF (text-mode open on Windows)."""
    import proskenion.core.secrets as module

    fixed = bytes([10, 13]) * 16

    class FakeSecrets:
        @staticmethod
        def token_bytes(n: int) -> bytes:
            return fixed[:n]

    monkeypatch.setattr(module, "_secrets", FakeSecrets)
    path = generate_secret_if_missing(tmp_path / "device-secret")
    assert path.read_bytes() == fixed
    assert DeviceSecret.load(path)


def test_load_rejects_wrong_size_or_missing(tmp_path: Path) -> None:
    short = tmp_path / "short"
    short.write_bytes(b"x" * 16)
    with pytest.raises(SecretUnavailable):
        DeviceSecret.load(short)
    with pytest.raises(SecretUnavailable):
        DeviceSecret.load(tmp_path / "absent")


def test_round_trip_transforms_only_encrypted_fields() -> None:
    secret = DeviceSecret(bytes(range(32)))
    values = {"password": "hunter2", "note": "front row", "pin": "1234"}
    stored = secret.encrypt_config(SCHEMA, values)
    assert is_encrypted_value(stored["password"])
    assert stored["note"] == "front row"
    assert stored["pin"] == "1234"
    assert "hunter2" not in repr(stored)
    assert secret.decrypt_config(SCHEMA, stored) == values


def test_encrypted_value_is_distinguishable_and_nonce_varies() -> None:
    secret = DeviceSecret(bytes(range(32)))
    one = secret.encrypt_value("password", "same")
    two = secret.encrypt_value("password", "same")
    assert one != two
    assert is_encrypted_value(one) and not is_encrypted_value("same")
    assert not is_encrypted_value({"enc": "x", "extra": 1})


def test_already_encrypted_value_is_kept_on_re_save() -> None:
    secret = DeviceSecret(bytes(range(32)))
    stored = secret.encrypt_config(SCHEMA, {"password": "hunter2"})
    again = secret.encrypt_config(SCHEMA, stored)
    assert again["password"] == stored["password"]
    assert secret.decrypt_config(SCHEMA, {"password": None, "note": None}) == {
        "password": None,
        "note": None,
    }


def test_wrong_key_raises_secret_mismatch_not_garbage() -> None:
    original = DeviceSecret(bytes(range(32)))
    replacement = DeviceSecret(bytes(range(32, 64)))  # restored to new hardware
    stored = original.encrypt_config(SCHEMA, {"password": "hunter2"})
    with pytest.raises(SecretMismatch, match="re-enter"):
        replacement.decrypt_config(SCHEMA, stored)


def test_tampered_or_malformed_values_raise_secret_mismatch() -> None:
    secret = DeviceSecret(bytes(range(32)))
    stored = secret.encrypt_value("password", "hunter2")
    blob = stored["enc"]
    tampered = {"enc": blob[:-4] + ("AAAA" if blob[-4:] != "AAAA" else "BBBB")}
    with pytest.raises(SecretMismatch):
        secret.decrypt_value("password", tampered)
    with pytest.raises(SecretMismatch):
        secret.decrypt_value("password", {"enc": "not base64!"})
    with pytest.raises(SecretMismatch):
        secret.decrypt_value("password", {"enc": "AAAA"})


def test_ciphertext_is_bound_to_its_field() -> None:
    secret = DeviceSecret(bytes(range(32)))
    stored = secret.encrypt_value("password", "hunter2")
    with pytest.raises(SecretMismatch):
        secret.decrypt_value("other_password", stored)


def test_only_text_can_be_encrypted() -> None:
    secret = DeviceSecret(bytes(range(32)))
    with pytest.raises(TypeError):
        secret.encrypt_config(SCHEMA, {"password": 1234})
