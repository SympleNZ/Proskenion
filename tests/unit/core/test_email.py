"""SMTP sending and the emergency fallback mirror (spec §11.4, §4.6).

Every send is proved against a real (if minimal) SMTP server — ``aiosmtpd``,
run in its own thread on a loopback port — never mocked, so a change to
``aiosmtplib``'s exception shapes would break these tests rather than pass
silently. Plain port 25 with no authentication (the shape of the real relay,
phase-6 plan Q1), opportunistic STARTTLS offered and not offered, implicit
TLS, AUTH success and failure, and a rejected recipient are each their own
server, because each is a different combination of what the server
advertises and what the client must therefore do.
"""

from __future__ import annotations

import contextlib
import socket
import ssl
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from aiosmtpd.controller import Controller
from aiosmtpd.smtp import AuthResult, LoginPassword

from proskenion.core.certs import generate_self_signed
from proskenion.core.email import (
    EmailSettings,
    SmtpError,
    SmtpFailureStage,
    fallback_path,
    read_fallback,
    send_email,
    write_fallback,
)
from proskenion.core.secrets import DeviceSecret

pytestmark = [
    pytest.mark.filterwarnings("ignore::DeprecationWarning"),
    # aiosmtpd's own security reminder about AUTH without mandatory TLS —
    # this suite tests the AUTH failure path itself, not TLS policy.
    pytest.mark.filterwarnings("ignore::UserWarning"),
]


# -- a minimal SMTP server -----------------------------------------------------------


class RecordingHandler:
    """Accepts everything except ``reject_recipient``; records what was sent."""

    def __init__(self, *, reject_recipient: str | None = None) -> None:
        self.envelopes: list[Any] = []
        self._reject_recipient = reject_recipient

    async def handle_RCPT(  # noqa: N802 - aiosmtpd's own naming convention
        self, server: Any, session: Any, envelope: Any, address: str, rcpt_options: Any
    ) -> str:
        if self._reject_recipient is not None and address == self._reject_recipient:
            return "550 5.1.1 mailbox unavailable"
        envelope.rcpt_tos.append(address)
        return "250 OK"

    async def handle_DATA(self, server: Any, session: Any, envelope: Any) -> str:  # noqa: N802
        self.envelopes.append(envelope)
        return "250 Message accepted for delivery"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _server_tls_context(tmp_path: Path) -> ssl.SSLContext:
    paths = generate_self_signed("localhost", directory=tmp_path / "server-cert")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(paths.certificate), str(paths.key))
    return context


def _authenticator(expected_user: str, expected_password: str) -> Any:
    def _authenticate(
        server: Any, session: Any, envelope: Any, mechanism: str, auth_data: Any
    ) -> AuthResult:
        if isinstance(auth_data, LoginPassword):
            ok = (
                auth_data.login.decode() == expected_user
                and auth_data.password.decode() == expected_password
            )
            # handled=False: aiosmtpd pushes its own 235/535 response rather
            # than waiting for this hook to have sent one itself.
            return AuthResult(success=ok, handled=False)
        return AuthResult(success=False, handled=False)

    return _authenticate


@contextlib.contextmanager
def smtp_server(handler: Any, **controller_kwargs: Any) -> Iterator[int]:
    port = _free_port()
    controller = Controller(handler, hostname="127.0.0.1", port=port, **controller_kwargs)
    controller.start()
    try:
        yield port
    finally:
        controller.stop()


def _settings(port: int, **overrides: Any) -> EmailSettings:
    base = dict(
        host="127.0.0.1",
        port=port,
        tls_mode="none",
        username=None,
        password=None,
        sender="controller@auditorium.school.nz",
        recipient="ict@obhs.school.nz",
    )
    base.update(overrides)
    return EmailSettings(**base)  # type: ignore[arg-type]


# -- plain, STARTTLS, implicit TLS, AUTH ----------------------------------------------


async def test_plain_port_25_no_auth_sends() -> None:
    """The exact shape of the real relay (Q1): no TLS, no login."""
    handler = RecordingHandler()
    with smtp_server(handler) as port:
        await send_email(_settings(port, tls_mode="none"), subject="hello", body="world")
    assert len(handler.envelopes) == 1
    assert handler.envelopes[0].mail_from == "controller@auditorium.school.nz"
    assert handler.envelopes[0].rcpt_tos == ["ict@obhs.school.nz"]


async def test_starttls_is_used_when_the_server_offers_it(tmp_path: Path) -> None:
    handler = RecordingHandler()
    tls_context = _server_tls_context(tmp_path)
    with smtp_server(handler, tls_context=tls_context) as port:
        await send_email(
            _settings(port, tls_mode="starttls"),
            subject="hello",
            body="world",
            validate_certs=False,  # a self-signed test certificate, not a trust question
        )
    assert len(handler.envelopes) == 1


async def test_starttls_is_skipped_when_the_server_does_not_offer_it() -> None:
    """Opportunistic (§11.4, the default TLS mode): no STARTTLS advertised,
    so the message still sends in the clear — this is what makes the
    unauthenticated, TLS-less relay of Q1 work with the default configuration."""
    handler = RecordingHandler()
    with smtp_server(handler) as port:  # no tls_context: STARTTLS is not offered
        await send_email(_settings(port, tls_mode="starttls"), subject="hello", body="world")
    assert len(handler.envelopes) == 1


async def test_implicit_tls_sends(tmp_path: Path) -> None:
    handler = RecordingHandler()
    tls_context = _server_tls_context(tmp_path)
    with smtp_server(handler, ssl_context=tls_context) as port:
        await send_email(
            _settings(port, tls_mode="tls"),
            subject="hello",
            body="world",
            validate_certs=False,
        )
    assert len(handler.envelopes) == 1


async def test_auth_success() -> None:
    handler = RecordingHandler()
    authenticator = _authenticator("relay-user", "correct horse")
    with smtp_server(
        handler, auth_required=True, auth_require_tls=False, authenticator=authenticator
    ) as port:
        await send_email(
            _settings(port, username="relay-user", password="correct horse"),
            subject="hello",
            body="world",
        )
    assert len(handler.envelopes) == 1


async def test_auth_failure_is_reported_as_the_auth_stage() -> None:
    handler = RecordingHandler()
    authenticator = _authenticator("relay-user", "correct horse")
    with smtp_server(
        handler, auth_required=True, auth_require_tls=False, authenticator=authenticator
    ) as port:
        with pytest.raises(SmtpError) as excinfo:
            await send_email(
                _settings(port, username="relay-user", password="wrong password"),
                subject="hello",
                body="world",
            )
    assert excinfo.value.stage is SmtpFailureStage.AUTH
    assert handler.envelopes == []


async def test_rejected_recipient_is_reported() -> None:
    handler = RecordingHandler(reject_recipient="ict@obhs.school.nz")
    with smtp_server(handler) as port:
        with pytest.raises(SmtpError) as excinfo:
            await send_email(_settings(port), subject="hello", body="world")
    assert excinfo.value.stage is SmtpFailureStage.REJECTED_RECIPIENT
    assert handler.envelopes == []


async def test_dns_failure_is_reported() -> None:
    with pytest.raises(SmtpError) as excinfo:
        await send_email(
            _settings(25, host="this-host-does-not-exist.invalid.example"),
            subject="hello",
            body="world",
            timeout_s=5.0,
        )
    assert excinfo.value.stage is SmtpFailureStage.DNS


async def test_connect_failure_is_reported() -> None:
    """A resolvable address with nothing listening — refused, not "no such host"."""
    unused_port = _free_port()  # bound-and-released; nothing is listening now
    with pytest.raises(SmtpError) as excinfo:
        await send_email(
            _settings(unused_port, host="127.0.0.1"),
            subject="hello",
            body="world",
            timeout_s=5.0,
        )
    assert excinfo.value.stage is SmtpFailureStage.CONNECT


async def test_high_priority_sets_the_urgent_headers() -> None:
    handler = RecordingHandler()
    with smtp_server(handler) as port:
        await send_email(
            _settings(port), subject="rollback", body="it happened", priority="high"
        )
    content = bytes(handler.envelopes[0].content)
    assert b"X-Priority: 1" in content
    assert b"Importance: high" in content


async def test_normal_priority_sets_no_urgent_headers() -> None:
    handler = RecordingHandler()
    with smtp_server(handler) as port:
        await send_email(_settings(port), subject="fyi", body="just so you know")
    content = bytes(handler.envelopes[0].content)
    assert b"X-Priority" not in content


# -- the emergency fallback mirror (§4.6) ----------------------------------------------


@pytest.fixture
def secret() -> DeviceSecret:
    return DeviceSecret(key=bytes(range(32)))


def test_write_fallback_then_read_fallback_round_trips(
    tmp_path: Path, secret: DeviceSecret
) -> None:
    settings = EmailSettings(
        host="relay.n4l.co.nz",
        port=25,
        tls_mode="starttls",
        username="svc-account",
        password="hunter2",
        sender="controller@auditorium.school.nz",
        recipient="ict@obhs.school.nz",
    )
    write_fallback(tmp_path, settings, secret)
    restored = read_fallback(tmp_path, secret)
    assert restored == settings


def test_write_fallback_with_no_password(tmp_path: Path, secret: DeviceSecret) -> None:
    settings = EmailSettings(
        host="relay.n4l.co.nz",
        port=25,
        tls_mode="none",
        username=None,
        password=None,
        sender="a@school.nz",
        recipient="b@school.nz",
    )
    write_fallback(tmp_path, settings, secret)
    restored = read_fallback(tmp_path, secret)
    assert restored == settings


def test_fallback_file_mode_is_0600(tmp_path: Path, secret: DeviceSecret) -> None:
    settings = EmailSettings(
        host="relay.n4l.co.nz", port=25, tls_mode="none", username=None, password=None,
        sender="a@school.nz", recipient="b@school.nz",
    )
    path = write_fallback(tmp_path, settings, secret)
    assert path == fallback_path(tmp_path)
    mode = stat.S_IMODE(path.stat().st_mode)
    # Windows has no POSIX mode bits to check; this asserts what matters
    # everywhere the appliance actually runs.
    if hasattr(__import__("os"), "chmod") and __import__("sys").platform != "win32":
        assert mode == 0o600


def test_fallback_never_stores_the_password_in_the_clear(
    tmp_path: Path, secret: DeviceSecret
) -> None:
    settings = EmailSettings(
        host="relay.n4l.co.nz", port=25, tls_mode="starttls", username="svc",
        password="correct horse battery staple", sender="a@school.nz", recipient="b@school.nz",
    )
    path = write_fallback(tmp_path, settings, secret)
    raw = path.read_text(encoding="utf-8")
    assert "correct horse battery staple" not in raw


def test_read_fallback_with_no_file_returns_none(tmp_path: Path, secret: DeviceSecret) -> None:
    assert read_fallback(tmp_path, secret) is None


def test_read_fallback_with_a_malformed_file_returns_none(
    tmp_path: Path, secret: DeviceSecret
) -> None:
    fallback_path(tmp_path).write_text("not valid toml {{{", encoding="utf-8")
    assert read_fallback(tmp_path, secret) is None


def test_read_fallback_with_a_different_device_secret_drops_only_the_password(
    tmp_path: Path, secret: DeviceSecret
) -> None:
    settings = EmailSettings(
        host="relay.n4l.co.nz", port=25, tls_mode="starttls", username="svc",
        password="hunter2", sender="a@school.nz", recipient="b@school.nz",
    )
    write_fallback(tmp_path, settings, secret)
    other_secret = DeviceSecret(key=bytes(range(32, 64)))
    restored = read_fallback(tmp_path, other_secret)
    assert restored is not None
    assert restored.password is None
    assert restored.host == settings.host and restored.recipient == settings.recipient
