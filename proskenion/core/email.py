"""SMTP: sending a message (spec §11.4) and the emergency fallback mirror (§4.6).

Two independent things live here because they share one shape,
:class:`EmailSettings`, and one failure vocabulary:

``send_email``
    Sends one message over ``aiosmtplib``, classifying whatever goes wrong
    into a closed set of stages — :class:`SmtpFailureStage` — so
    ``POST /system/email/test`` (proskenion/api/system.py) can name the
    failure rather than show a raw exception, and so
    :class:`proskenion.core.alerts.SmtpAlertSink` can log something useful
    without ever raising into its caller.

    The default TLS mode, ``starttls``, is *opportunistic*: it is
    ``aiosmtplib``'s own ``start_tls=None``, which upgrades the connection
    only if the server advertises ``STARTTLS`` and otherwise sends in the
    clear. The relay this phase ships against — ``relay.n4l.co.nz``, port 25,
    no authentication (phase-6 plan, Q1) — needs exactly that: a relay
    reachable only from the school network, offering neither TLS nor a
    login, must still work with the *default* configuration, not a special
    case an admin has to discover. ``tls_mode = "tls"`` (implicit TLS from
    the first byte) and ``"none"`` (never even try) are there for a relay
    that needs one or the other.

``write_fallback`` / ``read_fallback``
    Mirrors the last configuration that actually sent an email to
    ``/srv/appliance/smtp-fallback.toml``, mode 0600, so degraded/emergency
    mode (§4.6) can still raise an alert with no database and no
    application running. "Last-known-good only" (contracts §7): the caller —
    :class:`~proskenion.core.alerts.SmtpAlertSink` and the
    ``POST /system/email/test`` handler — calls :func:`write_fallback` only
    after :func:`send_email` has *not* raised; a configuration that has never
    proven it can send is never written here, so emergency mode never tries
    something new. ``read_fallback`` is deliberately small and reads nothing
    but the filesystem, the stdlib TOML parser and the device secret, because
    it has to work in exactly the conditions that make everything else
    (the database, the running application) unavailable.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import socket
import ssl
import tomllib
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from enum import StrEnum
from pathlib import Path
from typing import Final, Literal

import aiosmtplib
from aiosmtplib.errors import (
    SMTPAuthenticationError,
    SMTPConnectError,
    SMTPException,
    SMTPRecipientRefused,
    SMTPRecipientsRefused,
)

from proskenion.core.drivers.fields import ENCRYPTED_KEY
from proskenion.core.secrets import DeviceSecret, SecretMismatch

log = logging.getLogger(__name__)

TlsMode = Literal["none", "starttls", "tls"]
TLS_MODES: Final[tuple[TlsMode, ...]] = ("none", "starttls", "tls")
DEFAULT_TLS_MODE: Final[TlsMode] = "starttls"

#: A relay reachable only from the school network (Q1); this is generous
#: enough to survive a slow morning without leaving a background alert send
#: hanging for the default 60 s aiosmtplib would otherwise wait.
SEND_TIMEOUT_S: Final = 15.0


@dataclass(frozen=True, slots=True)
class EmailSettings:
    """The SMTP relay and message envelope — never stored in the database in
    this shape; ``proskenion.db.crud.email`` stores the password encrypted
    and this is only ever built after decrypting it (§6.10)."""

    host: str
    port: int
    tls_mode: TlsMode
    username: str | None
    password: str | None
    sender: str
    recipient: str


class SmtpFailureStage(StrEnum):
    """Where a send failed — closed, so the test endpoint and the alert log
    always name one of these rather than echoing a raw exception (§16.1's
    closed-vocabulary rule extended to this domain)."""

    DNS = "dns"
    CONNECT = "connect"
    TLS = "tls"
    AUTH = "auth"
    REJECTED_RECIPIENT = "rejected_recipient"


class SmtpError(Exception):
    """A send failed at :attr:`stage`. ``detail`` is safe to show — it never
    carries the password, which never reaches an exception message because
    it never reaches a log line or a response either."""

    def __init__(self, stage: SmtpFailureStage, detail: str) -> None:
        super().__init__(f"{stage.value}: {detail}")
        self.stage = stage
        self.detail = detail


def _build_message(
    settings: EmailSettings, subject: str, body: str, priority: str
) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = settings.sender
    message["To"] = settings.recipient
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain="proskenion.invalid")
    if priority == "high":
        # No MTA is obliged to honour these, but ict@obhs.school.nz's mail
        # client likely will, and a rollback alert (a seam for an automatic
        # rollback) should sort to the top of a Monday-morning inbox.
        message["X-Priority"] = "1"
        message["Importance"] = "high"
    message.set_content(body)
    return message


async def _quiet_quit(client: aiosmtplib.SMTP) -> None:
    with contextlib.suppress(Exception):
        await client.quit()


async def send_email(
    settings: EmailSettings,
    *,
    subject: str,
    body: str,
    priority: str = "normal",
    timeout_s: float = SEND_TIMEOUT_S,
    validate_certs: bool = True,
) -> None:
    """Send one message. Raises :class:`SmtpError` naming the stage; never a
    raw ``aiosmtplib`` exception, so every caller can log or report the
    failure without knowing that library's vocabulary.

    ``validate_certs`` defaults to the secure choice and is not reachable
    from ``Admin -> System -> Email`` — it exists so the unit tests can point
    at ``aiosmtpd``'s self-signed test certificate without weakening what the
    application itself ever does.
    """
    message = _build_message(settings, subject, body, priority)
    client = aiosmtplib.SMTP(
        hostname=settings.host,
        port=settings.port,
        timeout=timeout_s,
        use_tls=settings.tls_mode == "tls",
        # None: aiosmtplib's own opportunistic upgrade — see the module
        # docstring. False for "none" disables it outright.
        start_tls=None if settings.tls_mode == "starttls" else False,
        validate_certs=validate_certs,
    )
    try:
        await client.connect()
    except SMTPConnectError as exc:
        # aiosmtplib wraps a DNS failure in SMTPConnectError too; the
        # original socket.gaierror survives as __cause__ (confirmed against
        # aiosmtplib 5.1), which is the only way to tell "the name does not
        # resolve" apart from "the resolved address refused the connection".
        if isinstance(exc.__cause__, socket.gaierror):
            raise SmtpError(SmtpFailureStage.DNS, str(exc)) from exc
        raise SmtpError(SmtpFailureStage.CONNECT, str(exc)) from exc
    except ssl.SSLError as exc:
        raise SmtpError(SmtpFailureStage.TLS, str(exc)) from exc
    except SMTPException as exc:
        # A STARTTLS handshake that was attempted (because the server
        # advertised it) and then failed lands here, not in SMTPConnectError.
        raise SmtpError(SmtpFailureStage.TLS, str(exc)) from exc
    try:
        try:
            if settings.username:
                await client.login(settings.username, settings.password or "")
        except SMTPAuthenticationError as exc:
            raise SmtpError(SmtpFailureStage.AUTH, str(exc)) from exc
        try:
            await client.send_message(message)
        except (SMTPRecipientsRefused, SMTPRecipientRefused) as exc:
            raise SmtpError(SmtpFailureStage.REJECTED_RECIPIENT, str(exc)) from exc
        except SMTPException as exc:
            raise SmtpError(SmtpFailureStage.CONNECT, str(exc)) from exc
    finally:
        await _quiet_quit(client)


# -- the emergency fallback mirror (§4.6, contracts §7) -----------------------------

FALLBACK_FILENAME: Final = "smtp-fallback.toml"
FALLBACK_MODE: Final = 0o600
_FALLBACK_PASSWORD_FIELD: Final = "smtp_fallback_password"


def fallback_path(state_dir: Path | str) -> Path:
    """``<state_dir>/smtp-fallback.toml`` — ``/srv/appliance`` on the appliance."""
    return Path(state_dir) / FALLBACK_FILENAME


def _toml_string(value: str) -> str:
    """A TOML basic string literal. A JSON string literal is one: both use
    ``"``-quoting with the same backslash escapes for control characters,
    ``\\`` and ``"`` — there is no stdlib TOML *writer* to reach for instead,
    and every value mirrored here is a short host, address or opaque token,
    never text a JSON escaper would render differently from TOML's own.
    """
    return json.dumps(value)


def _write_private(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically, mode 0600 exactly (§6.10 model:
    ``core/certs.py``'s ``_write_exact``, the same pattern for a different mode)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0)
    fd = os.open(temporary, flags, FALLBACK_MODE)
    try:
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)
    os.chmod(temporary, FALLBACK_MODE)  # O_CREAT's mode is masked by umask
    os.replace(temporary, path)
    os.chmod(path, FALLBACK_MODE)


def write_fallback(state_dir: Path | str, settings: EmailSettings, secret: DeviceSecret) -> Path:
    """Mirror ``settings`` as the last-known-good relay. See the module docstring."""
    lines = [
        "# Written by proskenion after a message actually sent (§11.4, §4.6).",
        "# Read by emergency mode with no database and no application running —",
        "# see proskenion.core.email.read_fallback. Edits here are overwritten.",
        f"host = {_toml_string(settings.host)}",
        f"port = {int(settings.port)}",
        f"tls_mode = {_toml_string(settings.tls_mode)}",
        f"username = {_toml_string(settings.username or '')}",
    ]
    if settings.password:
        encrypted = secret.encrypt_value(_FALLBACK_PASSWORD_FIELD, settings.password)
        lines.append(f"password_enc = {_toml_string(encrypted[ENCRYPTED_KEY])}")
    else:
        lines.append('password_enc = ""')
    lines.append(f"sender = {_toml_string(settings.sender)}")
    lines.append(f"recipient = {_toml_string(settings.recipient)}")
    path = fallback_path(state_dir)
    _write_private(path, "\n".join(lines) + "\n")
    return path


def read_fallback(state_dir: Path | str, secret: DeviceSecret) -> EmailSettings | None:
    """The mirrored last-known-good relay, or ``None`` if nothing has ever
    sent successfully, the file is absent, or it cannot be parsed. Never
    raises: a malformed or missing mirror is "no fallback", not a startup
    failure, in the one mode where a startup failure is what it exists to
    avoid.
    """
    path = fallback_path(state_dir)
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        log.info("no SMTP fallback at %s: %s", path, exc)
        return None
    try:
        host = str(raw["host"])
        port = int(raw["port"])
        tls_mode_raw = str(raw.get("tls_mode", DEFAULT_TLS_MODE))
        tls_mode: TlsMode = tls_mode_raw if tls_mode_raw in TLS_MODES else DEFAULT_TLS_MODE
        username = str(raw.get("username") or "") or None
        password_enc = str(raw.get("password_enc") or "")
        sender = str(raw["sender"])
        recipient = str(raw["recipient"])
    except (KeyError, TypeError, ValueError) as exc:
        log.warning("%s is malformed; emergency mode has no fallback relay: %s", path, exc)
        return None
    password: str | None = None
    if password_enc:
        try:
            password = secret.decrypt_value(_FALLBACK_PASSWORD_FIELD, {ENCRYPTED_KEY: password_enc})
        except SecretMismatch as exc:
            log.warning("%s: %s", path, exc)
            password = None
    return EmailSettings(
        host=host,
        port=port,
        tls_mode=tls_mode,
        username=username,
        password=password,
        sender=sender,
        recipient=recipient,
    )
