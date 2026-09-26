"""``smtp-fallback.toml``, root-side — read and send (§4.6, §11.4, contracts §7).

Reads ``/srv/appliance/smtp-fallback.toml``, the mirror
``proskenion.core.email.write_fallback`` keeps: the *last known good* relay,
written only after a real send actually succeeded, on a partition that
survives ``/data`` being unavailable. This is the sibling that reads it —
plain standard library (``tomllib``, ``smtplib``, ``ssl``, ``email``), so it
runs on the system Python on the read-only root with no venv and no
application, exactly the conditions emergency mode exists for.

Password decryption is the one place this needs more than the standard
library — see ``auditorium_device_secret`` — and it degrades to sending
unauthenticated, logging why, rather than failing outright: Q22's relay
(``relay.n4l.co.nz``, port 25, no authentication) needs no password at all,
so the common case never touches that path, and an uncommon relay that does
need one is better served by a plain-text attempt than by no alert at all.
"""

from __future__ import annotations

import email.utils
import logging
import smtplib
import ssl
import tomllib
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path

log = logging.getLogger("auditorium-emergency")

DEFAULT_PATH = Path("/srv/appliance/smtp-fallback.toml")
TLS_MODES: tuple[str, ...] = ("none", "starttls", "tls")
DEFAULT_TLS_MODE = "starttls"
SEND_TIMEOUT_S = 15.0
_PASSWORD_FIELD = "smtp_fallback_password"


@dataclass(frozen=True, slots=True)
class FallbackSettings:
    host: str
    port: int
    tls_mode: str
    username: str | None
    password: str | None
    sender: str
    recipient: str


def read(
    path: Path = DEFAULT_PATH, *, secret_path: Path | None = None
) -> FallbackSettings | None:
    """The mirrored relay, or ``None``.

    Never raises: a malformed or missing mirror is "no fallback", not a
    reason for the responder to fail — the one mode where a failure here is
    exactly what this file exists to avoid (module docstring,
    ``proskenion.core.email.read_fallback``'s sibling).
    """
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        log.info("no SMTP fallback at %s: %s", path, exc)
        return None
    try:
        host = str(raw["host"])
        port = int(raw["port"])
        tls_mode = str(raw.get("tls_mode", DEFAULT_TLS_MODE))
        if tls_mode not in TLS_MODES:
            tls_mode = DEFAULT_TLS_MODE
        username = str(raw.get("username") or "") or None
        password_enc = str(raw.get("password_enc") or "")
        sender = str(raw["sender"])
        recipient = str(raw["recipient"])
    except (KeyError, TypeError, ValueError) as exc:
        log.warning("%s is malformed; no fallback relay: %s", path, exc)
        return None
    password: str | None = None
    if password_enc:
        password = _decrypt_password(password_enc, secret_path)
    return FallbackSettings(
        host=host,
        port=port,
        tls_mode=tls_mode,
        username=username,
        password=password,
        sender=sender,
        recipient=recipient,
    )


def _decrypt_password(password_enc: str, secret_path: Path | None) -> str | None:
    try:
        import auditorium_device_secret as device_secret
    except ImportError:
        log.warning("no auditorium_device_secret on this root; sending the alert unauthenticated")
        return None
    try:
        key = device_secret.load_key(secret_path) if secret_path else device_secret.load_key()
        return device_secret.decrypt_value(key, _PASSWORD_FIELD, {"enc": password_enc})
    except (device_secret.SecretUnavailable, device_secret.SecretMismatch) as exc:
        log.warning("cannot decrypt the fallback password (%s); sending unauthenticated", exc)
        return None


def build_message(settings: FallbackSettings, subject: str, body: str) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = settings.sender
    message["To"] = settings.recipient
    message["Date"] = email.utils.formatdate(localtime=True)
    message["Message-ID"] = email.utils.make_msgid(domain="proskenion.invalid")
    # Emergency mode's one alert is the highest priority anything here sends.
    message["X-Priority"] = "1"
    message["Importance"] = "high"
    message.set_content(body)
    return message


def send(
    settings: FallbackSettings, *, subject: str, body: str, timeout_s: float = SEND_TIMEOUT_S
) -> None:
    """Send one message with ``smtplib`` — synchronous and standard library
    only, unlike ``proskenion.core.email.send_email``'s ``aiosmtplib``.

    Raises on failure (``OSError``, ``smtplib.SMTPException``); the caller
    logs rather than letting it propagate, since a failed alert must never
    stop the responder from serving ``/health``.
    """
    message = build_message(settings, subject, body)
    if settings.tls_mode == "tls":
        with smtplib.SMTP_SSL(
            settings.host, settings.port, timeout=timeout_s, context=ssl.create_default_context()
        ) as client:
            _authenticate_and_send(client, settings, message)
        return
    with smtplib.SMTP(settings.host, settings.port, timeout=timeout_s) as client:
        client.ehlo()
        # Opportunistic, like proskenion.core.email's default: upgrade only if
        # the server offers it, and never demand it of Q22's relay, which does
        # not (relay.n4l.co.nz, port 25, no authentication).
        if settings.tls_mode == "starttls" and client.has_extn("starttls"):
            client.starttls(context=ssl.create_default_context())
            client.ehlo()
        _authenticate_and_send(client, settings, message)


def _authenticate_and_send(
    client: smtplib.SMTP, settings: FallbackSettings, message: EmailMessage
) -> None:
    if settings.username:
        client.login(settings.username, settings.password or "")
    client.send_message(message)
