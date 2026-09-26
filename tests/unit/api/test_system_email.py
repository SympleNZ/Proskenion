"""``GET``/``PUT /system/email`` and ``POST /system/email/test`` (contracts §5, §7).

The test endpoint runs against a real ``aiosmtpd`` stub — the same pattern
as ``tests/unit/core/test_email.py`` — rather than mocking ``send_email``,
so the "success" and "primary failure" cases (§22.4) exercise the actual
network path the admin's test button relies on.
"""

from __future__ import annotations

import errno
import json
import os
import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from aiosmtpd.controller import Controller
from httpx import AsyncClient, Response

from proskenion.api.app import API_PREFIX
from proskenion.config import Config
from proskenion.core import email as email_module
from proskenion.core import system_config
from proskenion.core.email import FALLBACK_FILENAME, read_fallback
from proskenion.core.secrets import DEFAULT_SECRET_PATH, DeviceSecret, generate_secret_if_missing
from proskenion.db.connection import Database
from proskenion.db.crud import email as email_crud
from proskenion.db.crud import security_events
from tests.unit.api.conftest import ADMIN_PASSWORD, OPERATOR_PASSWORD
from tests.unit.appliance.test_firewall_ports import (
    image_system_json,
    load_generator,
    outbound,
    parse_rules,
)

SYSTEM = f"{API_PREFIX}/system"


async def login(client: AsyncClient, password: str = ADMIN_PASSWORD) -> Response:
    return await client.post(f"{API_PREFIX}/auth/login", json={"password": password})


# -- a minimal SMTP stub, as in tests/unit/core/test_email.py ------------------------


class _RecordingHandler:
    def __init__(self) -> None:
        self.envelopes: list[Any] = []

    async def handle_DATA(self, server: Any, session: Any, envelope: Any) -> str:  # noqa: N802
        self.envelopes.append(envelope)
        return "250 Message accepted for delivery"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture
def smtp_stub() -> Iterator[tuple[int, _RecordingHandler]]:
    handler = _RecordingHandler()
    port = _free_port()
    controller = Controller(handler, hostname="127.0.0.1", port=port)
    controller.start()
    try:
        yield port, handler
    finally:
        controller.stop()


VALID_BODY = {
    "host": "relay.n4l.co.nz",
    "port": 25,
    "tls_mode": "none",
    "sender": "controller@auditorium.school.nz",
    "recipient": "ict@obhs.school.nz",
}


# -- GET/PUT -----------------------------------------------------------------------


async def test_get_email_when_unconfigured(client: AsyncClient) -> None:
    await login(client)
    response = await client.get(f"{SYSTEM}/email")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body == {
        "host": None,
        "port": None,
        "tls_mode": "starttls",
        "username": None,
        "password_set": False,
        "sender": None,
        "recipient": None,
        "updated_at": None,
    }


async def test_get_email_requires_admin(client: AsyncClient) -> None:
    anonymous = await client.get(f"{SYSTEM}/email")
    assert anonymous.status_code == 401

    await login(client, OPERATOR_PASSWORD)
    operator = await client.get(f"{SYSTEM}/email")
    assert operator.status_code == 403


async def test_put_email_requires_admin(client: AsyncClient) -> None:
    await login(client, OPERATOR_PASSWORD)
    response = await client.put(f"{SYSTEM}/email", json=VALID_BODY)
    assert response.status_code == 403


async def test_put_email_saves_and_the_password_is_never_returned(
    client: AsyncClient, db: Database
) -> None:
    await login(client)
    response = await client.put(
        f"{SYSTEM}/email", json={**VALID_BODY, "username": "svc", "password": "hunter2"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert "password" not in body
    assert body["password_set"] is True
    assert body["host"] == "relay.n4l.co.nz"
    assert body["username"] == "svc"
    assert body["updated_at"] is not None

    row = await email_crud.get(db)
    assert row is not None
    assert row.password is not None
    assert row.password != {"password": "hunter2"}  # never stored in the clear

    events = await security_events.query(db, event_type="config_changed")
    assert any(e.detail and "\"setting\": \"email\"" in e.detail for e in events)
    # The password itself never lands in the audit trail.
    assert all(e.detail is None or "hunter2" not in e.detail for e in events)


async def test_put_email_with_no_password_leaves_the_stored_one_unchanged(
    client: AsyncClient, db: Database, state_dir: Path
) -> None:
    await login(client)
    first_body = {**VALID_BODY, "username": "svc", "password": "hunter2"}
    await client.put(f"{SYSTEM}/email", json=first_body)
    second_body = {**VALID_BODY, "port": 26, "username": "svc"}
    response = await client.put(f"{SYSTEM}/email", json=second_body)
    assert response.status_code == 200, response.text
    assert response.json()["password_set"] is True
    assert response.json()["port"] == 26

    secret_path = state_dir / DEFAULT_SECRET_PATH.name
    generate_secret_if_missing(secret_path)
    secret = DeviceSecret.load(secret_path)
    row = await email_crud.get(db)
    assert row is not None and row.password is not None
    assert secret.decrypt_value("email_password", row.password) == "hunter2"


async def test_put_email_rejects_an_invalid_body(client: AsyncClient) -> None:
    await login(client)
    response = await client.put(f"{SYSTEM}/email", json={**VALID_BODY, "tls_mode": "ssl3"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_failed"


# -- POST /system/email/test ---------------------------------------------------------


async def test_email_test_requires_admin(client: AsyncClient) -> None:
    await login(client, OPERATOR_PASSWORD)
    response = await client.post(f"{SYSTEM}/email/test", json={})
    assert response.status_code == 403


async def test_email_test_with_nothing_saved_and_nothing_posted(client: AsyncClient) -> None:
    await login(client)
    response = await client.post(f"{SYSTEM}/email/test", json={})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_failed"


async def test_email_test_succeeds_against_the_saved_configuration(
    client: AsyncClient, smtp_stub: tuple[int, _RecordingHandler], state_dir: Path
) -> None:
    port, handler = smtp_stub
    await login(client)
    await client.put(f"{SYSTEM}/email", json={**VALID_BODY, "host": "127.0.0.1", "port": port})

    response = await client.post(f"{SYSTEM}/email/test", json={})
    assert response.status_code == 200, response.text
    assert response.json() == {
        "ok": True,
        "stage": None,
        "message": "Test email sent",
        "fallback_saved": True,
    }
    assert len(handler.envelopes) == 1

    # Last-known-good (contracts §7): mirrored after a successful test.
    secret_path = state_dir / DEFAULT_SECRET_PATH.name
    generate_secret_if_missing(secret_path)
    secret = DeviceSecret.load(secret_path)
    mirrored = read_fallback(state_dir, secret)
    assert mirrored is not None
    assert mirrored.host == "127.0.0.1" and mirrored.port == port


async def test_email_test_reports_a_primary_connect_failure_inline(client: AsyncClient) -> None:
    """§22.4: the endpoint's primary failure path. Nothing is listening on
    this port, so the failure is a connection refusal, named as such."""
    unused_port = _free_port()
    await login(client)
    response = await client.post(
        f"{SYSTEM}/email/test",
        json={**VALID_BODY, "host": "127.0.0.1", "port": unused_port},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is False
    assert body["stage"] == "connect"
    # A draft relay is not in the firewall yet (§3.4): the admin is told why.
    assert "save these settings" in body["message"]


async def test_email_test_proves_a_draft_before_it_is_saved(
    client: AsyncClient, db: Database, smtp_stub: tuple[int, _RecordingHandler]
) -> None:
    port, handler = smtp_stub
    await login(client)
    response = await client.post(
        f"{SYSTEM}/email/test",
        json={**VALID_BODY, "host": "127.0.0.1", "port": port},
    )
    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True
    assert len(handler.envelopes) == 1
    # Nothing was persisted by the test alone.
    assert await email_crud.get(db) is None


async def test_a_fallback_that_cannot_be_saved_after_delivery_is_reported_not_a_500(
    client: AsyncClient,
    smtp_stub: tuple[int, _RecordingHandler],
    state_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """24 September 2026: the email arrived and the endpoint answered 500.
    ``write_fallback`` replaces the file by rename, and in sticky
    /srv/appliance a root-owned file refuses that with EPERM — reproduced
    here at the same call, since this machine has no sticky directories.
    (The systemd harness proves the image's real modes: systemd-cases.sh.)"""
    real_replace = os.replace

    def sticky_replace(src: Any, dst: Any) -> None:
        if str(dst).endswith(FALLBACK_FILENAME):
            raise PermissionError(errno.EPERM, "Operation not permitted", str(dst))
        real_replace(src, dst)

    monkeypatch.setattr(email_module.os, "replace", sticky_replace)
    port, handler = smtp_stub
    await login(client)
    response = await client.post(
        f"{SYSTEM}/email/test", json={**VALID_BODY, "host": "127.0.0.1", "port": port}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True, "the email was delivered; the test must say so"
    assert body["fallback_saved"] is False
    assert "fallback" in body["message"] and "not saved" in body["message"]
    assert len(handler.envelopes) == 1


# -- the firewall follows the saved relay (§3.4, contracts §4) -------------------------


def _seed_image_system_json(config: Config) -> Path:
    path = system_config.system_config_path(config.app.data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(image_system_json()), encoding="utf-8")
    return path


def _helper_requests(config: Config) -> list[str]:
    directory = config.app.data_dir / "run" / "helper"
    return [p.read_text(encoding="utf-8") for p in sorted(directory.glob("*.json"))]


async def test_saving_the_email_settings_opens_the_firewall_to_the_relay(
    client: AsyncClient, config: Config
) -> None:
    """Both sides of the hand-off: the endpoint writes ``network.smtp_relay``
    and asks for ``apply-network``; the real generator renders a tcp/25 rule
    to exactly the relay's addresses from what it wrote."""
    path = _seed_image_system_json(config)
    await login(client)
    response = await client.put(
        f"{SYSTEM}/email", json={**VALID_BODY, "host": "au-smtp-outbound-1.mimecast.com"}
    )
    assert response.status_code == 200, response.text

    network = system_config.read(config.app.data_dir)["network"]
    assert network["smtp_relay"] == {"host": "au-smtp-outbound-1.mimecast.com", "port": 25}
    assert network["address"] == image_system_json()["network"]["address"], "network lost a key"
    assert any('"apply-network"' in r for r in _helper_requests(config)), "firewall not re-rendered"

    generator = load_generator()
    relay = ["205.139.110.221"]
    parsed = generator.parse(
        json.loads(path.read_text(encoding="utf-8")),
        config_path=path,
        resolve=lambda host, port: relay if host == "au-smtp-outbound-1.mimecast.com" else [],
    )
    rules = parse_rules(generator.render_nft(parsed))
    assert outbound(rules, "205.139.110.221", "tcp", 25)


async def test_clearing_the_email_settings_clears_the_relay(
    client: AsyncClient, config: Config, db: Database
) -> None:
    _seed_image_system_json(config)
    await login(client)
    await client.put(f"{SYSTEM}/email", json=VALID_BODY)
    assert "smtp_relay" in system_config.read(config.app.data_dir)["network"]
    before = len(_helper_requests(config))

    response = await client.delete(f"{SYSTEM}/email")
    assert response.status_code == 204, response.text
    assert await email_crud.get(db) is None
    assert "smtp_relay" not in system_config.read(config.app.data_dir)["network"]
    assert len(_helper_requests(config)) == before + 1


async def test_clearing_the_email_settings_requires_admin(client: AsyncClient) -> None:
    await login(client, OPERATOR_PASSWORD)
    response = await client.delete(f"{SYSTEM}/email")
    assert response.status_code == 403
