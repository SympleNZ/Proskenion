"""Cloudflare's DNS API for the Let's Encrypt DNS-01 challenge (spec §3.2, Q7).

The school's zone is on Cloudflare with the proxy disabled (§3.2), so the
handful of calls issuance needs are: read the zone to prove the token works,
create the ``_acme-challenge`` TXT record, and remove it again once the ACME
server has validated it. Nothing else is called — the token is scoped to
"Zone → DNS → Edit" on the single zone (§3.2), and a broader token is a
misconfiguration this module cannot detect, only avoid needing.

Synchronous, deliberately
--------------------------
:mod:`proskenion.core.acme_client` runs the whole issuance — ACME protocol
plus these DNS calls — inside one :func:`asyncio.to_thread` worker (§5.3), the
same rule :mod:`proskenion.core.certs` already follows for certificate
generation: ``acme`` itself is built on ``requests`` and blocks regardless, so
running the DNS calls from the same thread avoids bouncing between the event
loop and a worker for every step. :meth:`CloudflareClient.verify_token`, used
directly from the token-test endpoint, is wrapped in ``asyncio.to_thread`` by
its caller instead.

``base_url`` exists so a test can point this at a stub standing in for
Cloudflare — see ``tests/integration/certs/``, which proves issuance against
Pebble with exactly such a stub. Nothing here talks to the real Cloudflare API
in a test.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

import httpx

log = logging.getLogger(__name__)

API_BASE: Final = "https://api.cloudflare.com/client/v4"
REQUEST_TIMEOUT_S: Final = 15.0
#: Cloudflare's minimum TTL; the record is short-lived anyway (§3.2).
CHALLENGE_TTL_S: Final = 120
ACME_CHALLENGE_LABEL: Final = "_acme-challenge"


class CloudflareError(Exception):
    """The Cloudflare API refused the request, or could not be reached."""


@dataclass(frozen=True, slots=True)
class TxtRecord:
    """A DNS-01 challenge record this client created, for later removal."""

    record_id: str
    zone_id: str
    name: str


def challenge_record_name(hostname: str) -> str:
    """``_acme-challenge.<hostname>`` — RFC 8555 §8.4's fixed label."""
    return f"{ACME_CHALLENGE_LABEL}.{hostname.strip('.')}"


class CloudflareClient:
    """The three calls DNS-01 issuance needs, over a synchronous ``httpx.Client``.

    ``token`` is the plain API token (already decrypted by the caller — see
    :mod:`proskenion.core.certs`'s token storage, §6.10-style encryption at
    rest with the device secret). It is used only as the ``Authorization``
    header and is never logged.
    """

    def __init__(
        self,
        token: str,
        *,
        base_url: str = API_BASE,
        timeout_s: float = REQUEST_TIMEOUT_S,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout_s,
            transport=transport,
            headers={"Authorization": f"Bearer {token}"},
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> CloudflareClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- token test (§21.24 "Test") ------------------------------------------

    def verify_token(self) -> bool:
        """True if the token authenticates and can list at least the one zone it is scoped to.

        Cloudflare's generic ``/user/tokens/verify`` endpoint confirms a
        token is *active*, but a token scoped only to "Zone → DNS → Edit"
        (§3.2's least-privilege requirement) has no ``user`` scope at all, so
        that endpoint is the wrong test. Listing zones is what issuance
        actually needs, so it is what "Test" proves.

        Raises :class:`CloudflareError` when Cloudflare itself refused the
        request (``success: false`` — a bad token answers exactly this way,
        with ``errors`` such as ``6003 Invalid request headers``), carrying
        Cloudflare's own codes and messages rather than swallowing them into
        a plain ``False`` — the wizard used to report this
        as "no Cloudflare zone owns …", which sent an administrator hunting
        through DNS for a token problem.
        """
        payload = self._request("GET", "/zones", params={"per_page": 1})
        if not payload.get("success"):
            raise CloudflareError(f"Cloudflare rejected the request: {_errors(payload)}")
        return isinstance(payload.get("result"), list)

    # -- zone lookup ----------------------------------------------------------

    def zone_id_for(self, hostname: str) -> str:
        """The zone owning ``hostname``, tried from the full name down to a registrable domain.

        ``av.school.nz`` is a subdomain (§3.2); the zone itself is usually
        ``school.nz``. Rather than guess where the registrable boundary is,
        every suffix is tried against Cloudflare's own zone list until one
        answers, stopping before the bare TLD.

        Only a *successful* response with an empty ``result`` means "not this
        suffix, try the next one" — an unsuccessful response (``success:
        false``) means Cloudflare refused the request outright (an invalid
        token, most often) and is raised immediately rather than tried
        suffix-by-suffix and eventually reported as "no zone owns this
        hostname": that message sent the school's
        administrator looking at DNS and zone ownership when the token they
        had pasted was simply wrong.
        """
        labels = hostname.strip(".").split(".")
        for start in range(len(labels) - 1):
            candidate = ".".join(labels[start:])
            payload = self._request("GET", "/zones", params={"name": candidate})
            if not payload.get("success"):
                raise CloudflareError(f"Cloudflare rejected the request: {_errors(payload)}")
            result = payload.get("result") or []
            if result:
                zone_id = result[0].get("id")
                if isinstance(zone_id, str):
                    return zone_id
        raise CloudflareError(f"no Cloudflare zone owns {hostname!r}")

    # -- the A record (§10.8's reconnection flow, contracts §5 step 3) --------

    def upsert_a_record(self, zone_id: str, hostname: str, address: str) -> None:
        """Point ``hostname``'s A record at ``address``.

        Used when a network change is applied and a Cloudflare token is
        configured: the appliance updates its own record *before* applying,
        so the hostname resolves to the new address as soon as the change
        takes effect, and the reconnection page can send the browser to the
        hostname rather than the bare new address. Updates the existing
        record if there is one — a fresh zone lookup every time, never a
        cached record id, since the zone can change hands between changes —
        and creates it otherwise. Not proxied: this is the appliance's own
        address on the school network, not something Cloudflare should sit
        in front of (§3.2).
        """
        existing = self._request(
            "GET", f"/zones/{zone_id}/dns_records", params={"type": "A", "name": hostname}
        )
        results = existing.get("result") or []
        record_id = results[0].get("id") if existing.get("success") and results else None
        payload = {"type": "A", "name": hostname, "content": address, "ttl": 60, "proxied": False}
        if isinstance(record_id, str):
            response = self._request(
                "PATCH", f"/zones/{zone_id}/dns_records/{record_id}", json=payload
            )
        else:
            response = self._request("POST", f"/zones/{zone_id}/dns_records", json=payload)
        if not response.get("success"):
            raise CloudflareError(f"Cloudflare refused the A record update: {_errors(response)}")

    # -- the DNS-01 record ------------------------------------------------------

    def create_txt_record(self, zone_id: str, hostname: str, value: str) -> TxtRecord:
        name = challenge_record_name(hostname)
        payload = self._request(
            "POST",
            f"/zones/{zone_id}/dns_records",
            json={"type": "TXT", "name": name, "content": value, "ttl": CHALLENGE_TTL_S},
        )
        if not payload.get("success"):
            raise CloudflareError(f"Cloudflare refused the TXT record: {_errors(payload)}")
        result = payload.get("result")
        record_id = result.get("id") if isinstance(result, dict) else None
        if not isinstance(record_id, str):
            raise CloudflareError("Cloudflare did not return a record id for the TXT record")
        return TxtRecord(record_id=record_id, zone_id=zone_id, name=name)

    def delete_txt_record(self, record: TxtRecord) -> None:
        """Best-effort cleanup: a failure here does not fail issuance, it only litters DNS."""
        try:
            payload = self._request(
                "DELETE", f"/zones/{record.zone_id}/dns_records/{record.record_id}"
            )
        except CloudflareError as exc:
            log.warning("could not remove the ACME TXT record %s: %s", record.name, exc)
            return
        if not payload.get("success"):
            log.warning(
                "Cloudflare refused to remove the ACME TXT record %s: %s",
                record.name,
                _errors(payload),
            )

    # -- transport --------------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise CloudflareError(f"cannot reach Cloudflare: {exc}") from exc
        try:
            data = response.json()
        except ValueError as exc:
            raise CloudflareError(
                f"Cloudflare returned a non-JSON response ({response.status_code})"
            ) from exc
        if not isinstance(data, dict):
            raise CloudflareError("Cloudflare returned an unexpected response shape")
        return data


def _one_error(error: Any) -> str:
    if not isinstance(error, dict):
        return str(error)
    code = error.get("code")
    message = error.get("message", "")
    # "6003 Invalid request headers" — Cloudflare's own code alongside its own
    # message, so a refusal is actionable rather than a bare sentence a reader
    # has to guess the cause of.
    return f"{code} {message}".strip() if code is not None else str(message)


def _errors(payload: Mapping[str, Any]) -> str:
    errors = payload.get("errors") or []
    if not errors:
        return "no detail given"
    return "; ".join(_one_error(error) for error in errors)
