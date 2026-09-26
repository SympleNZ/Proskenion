"""``auditorium_fallback_mail.read`` has to make sense of exactly what
``proskenion.core.email.write_fallback`` writes (see that module's own docstring) —
the mirror emergency mode reads with no database and no application. Proved
here the same way ``boot-state.json``'s two sides are: build the file with
the application's own writer, read it back with the root-side one.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import ModuleType

import pytest

from proskenion.core.email import EmailSettings, write_fallback
from proskenion.core.secrets import DeviceSecret


def test_reads_back_a_relay_with_no_password(tmp_path: Path, fallback_mail: ModuleType) -> None:
    settings = EmailSettings(
        host="relay.n4l.co.nz",
        port=25,
        tls_mode="starttls",
        username=None,
        password=None,
        sender="auditorium@school.nz",
        recipient="ict@obhs.school.nz",
    )
    write_fallback(tmp_path, settings, DeviceSecret(os.urandom(32)))

    read_back = fallback_mail.read(tmp_path / "smtp-fallback.toml")
    assert read_back is not None
    assert read_back.host == "relay.n4l.co.nz"
    assert read_back.port == 25
    assert read_back.tls_mode == "starttls"
    assert read_back.username is None
    assert read_back.password is None
    assert read_back.sender == "auditorium@school.nz"
    assert read_back.recipient == "ict@obhs.school.nz"


def test_reads_back_a_relay_with_a_password(tmp_path: Path, fallback_mail: ModuleType) -> None:
    pytest.importorskip("cryptography")
    secret_path = tmp_path / "device-secret"
    secret_path.write_bytes(os.urandom(32))
    real_secret = DeviceSecret(secret_path.read_bytes())
    settings = EmailSettings(
        host="smtp.example.com",
        port=587,
        tls_mode="tls",
        username="auditorium",
        password="hunter2",
        sender="auditorium@school.nz",
        recipient="ict@obhs.school.nz",
    )
    write_fallback(tmp_path, settings, real_secret)

    read_back = fallback_mail.read(tmp_path / "smtp-fallback.toml", secret_path=secret_path)
    assert read_back is not None
    assert read_back.username == "auditorium"
    assert read_back.password == "hunter2"


def test_a_password_that_cannot_be_decrypted_degrades_to_none(
    tmp_path: Path, fallback_mail: ModuleType
) -> None:
    """A device-secret mismatch (restored to different hardware, §6.10) must
    not stop the fallback alert — it sends unauthenticated instead."""
    secret_path = tmp_path / "device-secret"
    secret_path.write_bytes(os.urandom(32))
    written_with = DeviceSecret(os.urandom(32))  # a different secret
    settings = EmailSettings(
        host="smtp.example.com",
        port=587,
        tls_mode="tls",
        username="auditorium",
        password="hunter2",
        sender="a@b",
        recipient="c@d",
    )
    write_fallback(tmp_path, settings, written_with)

    read_back = fallback_mail.read(tmp_path / "smtp-fallback.toml", secret_path=secret_path)
    assert read_back is not None
    assert read_back.password is None  # degraded, not raised


def test_missing_file_is_none(tmp_path: Path, fallback_mail: ModuleType) -> None:
    assert fallback_mail.read(tmp_path / "does-not-exist.toml") is None


def test_malformed_file_is_none(tmp_path: Path, fallback_mail: ModuleType) -> None:
    path = tmp_path / "smtp-fallback.toml"
    path.write_text("this is not [valid toml", encoding="utf-8")
    assert fallback_mail.read(path) is None

    path.write_text('host = "x"\n', encoding="utf-8")  # missing required keys
    assert fallback_mail.read(path) is None
