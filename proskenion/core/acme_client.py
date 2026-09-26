"""In-process ACME issuance over DNS-01 (spec §3.2, §21.24, Q7).

Certbot ships the protocol as a library (``acme``, with ``josepy`` for the
JWS envelope); Q7 chose issuing through that library in-process over shelling
out to the ``certbot`` binary, because §21.24 wants the five ACME phases of
the six-step Certificates card reported live as they happen, and a
subprocess's stdout is not a progress channel. Cloudflare is not mentioned
here at all — this module answers a DNS-01 challenge through whatever
``publish``/``remove`` callables it is given, and
:mod:`proskenion.core.cloudflare` is one such implementation, swapped for a
stub in ``tests/integration/certs/`` where issuance runs against Pebble.

Blocking, deliberately
-----------------------
``acme``'s ``ClientNetwork`` is built on ``requests``, so every ACME call
blocks regardless of the caller. :func:`request_certificate` is therefore a
plain (non-async) function, and its caller — :mod:`proskenion.core.certs` —
is the one place that wraps the whole issuance in :func:`asyncio.to_thread`
(§5.3). ``on_phase`` is called from that worker thread; a caller that needs
to reach the event loop from it (to publish a ``progress`` frame) marshals
back with ``loop.call_soon_threadsafe``, which is exactly what
:class:`~proskenion.core.certs.CertificateManager` does.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from acme import challenges, client, crypto_util, messages
from acme import errors as acme_errors
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from josepy.jwk import JWKRSA

log = logging.getLogger(__name__)

#: Let's Encrypt's production directory (§3.2). Pebble is pointed at its own
#: directory URL instead — never hard-coded past this default.
LETS_ENCRYPT_DIRECTORY_URL: Final = "https://acme-v02.api.letsencrypt.org/directory"
ACCOUNT_KEY_SIZE: Final = 2048
CERT_KEY_SIZE: Final = 2048
USER_AGENT: Final = "proskenion-acme-client"
#: How long polling for validation and finalisation is allowed to take
#: (RFC 8555's own retry backoff governs the polling interval within it).
FINALIZE_TIMEOUT_S: Final = 90.0

Phase = Literal["requesting", "dns_record", "propagation", "verifying", "downloaded"]
#: The five ACME-side phases, in order; certs.py adds a sixth of its own
#: ("reloading nginx") once the certificate is on disk.
PHASES: Final[tuple[Phase, ...]] = (
    "requesting",
    "dns_record",
    "propagation",
    "verifying",
    "downloaded",
)

PublishChallenge = Callable[[str, str], object]
"""``(hostname, validation_value) -> token`` — creates the TXT record and
returns whatever :data:`RemoveChallenge` needs to remove it again."""
RemoveChallenge = Callable[[object], None]
WaitForPropagation = Callable[[], None]
"""Called once the record is created, before telling the ACME server to
validate. The real implementation sleeps; a test can poll a stub instead."""
PhaseCallback = Callable[[Phase], None]


class AcmeError(Exception):
    """Issuance could not complete."""


@dataclass(frozen=True, slots=True)
class IssuedCertificate:
    """What issuance produced: the chain to serve, the key it was issued for, and its identity."""

    fullchain_pem: bytes
    key_pem: bytes
    issuer: str
    serial: str
    not_before: dt.datetime
    not_after: dt.datetime


def load_or_create_account_key(path: Path) -> bytes:
    """The ACME account's own key (never the certificate key), persisted under ``path``.

    Kept under ``/data/certs/`` rather than ``/srv/appliance`` alongside the
    device secret: it identifies this installation to Let's Encrypt, not a
    credential to another party, and losing it only costs a fresh
    registration, not a compromised secret.
    """
    path = Path(path)
    if path.exists():
        return path.read_bytes()
    key = rsa.generate_private_key(public_exponent=65537, key_size=ACCOUNT_KEY_SIZE)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pem)
    try:
        path.chmod(0o600)
    except OSError:  # pragma: no cover - platforms without POSIX modes
        pass
    return pem


def _build_client(
    directory_url: str, account_key_pem: bytes, *, verify_ssl: bool, timeout_s: float
) -> client.ClientV2:
    key = JWKRSA.load(account_key_pem)
    net = client.ClientNetwork(
        key, user_agent=USER_AGENT, verify_ssl=verify_ssl, timeout=int(timeout_s)
    )
    directory = client.ClientV2.get_directory(directory_url, net)
    return client.ClientV2(directory, net=net)


def _register(acme_client: client.ClientV2, contact_email: str | None) -> None:
    registration = messages.NewRegistration.from_data(
        email=contact_email, terms_of_service_agreed=True
    )
    try:
        acme_client.new_account(registration)
    except acme_errors.ConflictError:
        # Already registered under this account key — expected on renewal.
        pass


def _dns01_challenge(
    authorization: messages.AuthorizationResource,
) -> messages.ChallengeBody:
    for challb in authorization.body.challenges:
        if isinstance(challb.chall, challenges.DNS01):
            return challb
    raise AcmeError("the ACME server offered no dns-01 challenge for this order")


def request_certificate(
    hostname: str,
    *,
    account_key_pem: bytes,
    directory_url: str = LETS_ENCRYPT_DIRECTORY_URL,
    contact_email: str | None = None,
    publish_challenge: PublishChallenge,
    remove_challenge: RemoveChallenge,
    wait_for_propagation: WaitForPropagation,
    on_phase: PhaseCallback | None = None,
    verify_ssl: bool = True,
    timeout_s: float = FINALIZE_TIMEOUT_S,
    key_size: int = CERT_KEY_SIZE,
) -> IssuedCertificate:
    """Run one full DNS-01 issuance for ``hostname``. Blocking — see the module docstring.

    ``publish_challenge``/``remove_challenge`` are the DNS seam: production
    passes :meth:`~proskenion.core.cloudflare.CloudflareClient.create_txt_record`
    and ``delete_txt_record`` (adapted to this narrower signature by the
    caller); a test passes a stub that also programs Pebble's fake DNS
    resolver. The TXT record is always removed, success or failure.
    """

    def _phase(name: Phase) -> None:
        if on_phase is not None:
            on_phase(name)

    _phase("requesting")
    acme_client = _build_client(
        directory_url, account_key_pem, verify_ssl=verify_ssl, timeout_s=timeout_s
    )
    _register(acme_client, contact_email)

    cert_key = rsa.generate_private_key(public_exponent=65537, key_size=key_size)
    cert_key_pem = cert_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    csr_pem = crypto_util.make_csr(cert_key_pem, [hostname])

    try:
        order = acme_client.new_order(csr_pem)

        _phase("dns_record")
        tokens: list[object] = []
        try:
            for authorization in order.authorizations:
                challb = _dns01_challenge(authorization)
                _response, validation = challb.chall.response_and_validation(
                    acme_client.net.key
                )
                tokens.append(publish_challenge(hostname, validation))

            _phase("propagation")
            wait_for_propagation()

            _phase("verifying")
            for authorization in order.authorizations:
                challb = _dns01_challenge(authorization)
                response, _validation = challb.chall.response_and_validation(
                    acme_client.net.key
                )
                acme_client.answer_challenge(challb, response)

            deadline = dt.datetime.now() + dt.timedelta(seconds=timeout_s)
            finalized = acme_client.poll_and_finalize(order, deadline)
        finally:
            for token in tokens:
                remove_challenge(token)
    except acme_errors.Error as exc:
        raise AcmeError(f"Let's Encrypt issuance failed: {exc}") from exc

    _phase("downloaded")
    if not finalized.fullchain_pem:
        raise AcmeError("the ACME server finalised the order but returned no certificate")
    fullchain = finalized.fullchain_pem.encode("utf-8")
    leaf = x509.load_pem_x509_certificates(fullchain)[0]  # the leaf is first, then the chain
    return IssuedCertificate(
        fullchain_pem=fullchain,
        key_pem=cert_key_pem,
        issuer=_common_name(leaf.issuer) or leaf.issuer.rfc4514_string(),
        serial=format(leaf.serial_number, "x"),
        not_before=leaf.not_valid_before_utc,
        not_after=leaf.not_valid_after_utc,
    )


def _common_name(name: x509.Name) -> str | None:
    values = name.get_attributes_for_oid(NameOID.COMMON_NAME)
    if not values:
        return None
    value = values[0].value
    return value if isinstance(value, str) else value.decode("utf-8", "replace")
