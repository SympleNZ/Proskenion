"""Certificate management endpoints (contracts §5 `/system/certs/*`, §21.24).

Everything here defers to :class:`proskenion.core.certs.CertificateManager`,
built once in the application's lifespan and reached through
:func:`get_certs`, the same pattern :mod:`proskenion.api.system` uses for the
health poller and the time-sync monitor. ``POST /system/certs/issue`` streams
its six §21.24 steps as ``progress`` frames over the WebSocket rather than in
its HTTP response, so the response itself is just the finished card.
``POST /system/certs/self-signed`` (contracts §5, wave 3 additions) is the
way back when issuance cannot work at all — an expired token, no DNS, a site
off the internet — so, unlike ``/issue``, it reaches neither Cloudflare nor
an ACME server and has no steps to stream.

``GET /system/certs/download`` is deliberately **not** behind
``require_admin`` (contracts §5 marks it "public"): §6.16 is explicit that
trusting the certificate is something a hirer's iPad does before it can
authenticate at all, so the download has to be reachable without a session
(the Phase 5 carry-forward this closes out). It stays under ``/api/v1`` like
every other path here — "public" means no session, not unversioned — and
serves the certificate only, never the key, as ``application/x-pem-file``.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict

from proskenion.api.deps import get_config, require_admin
from proskenion.api.errors import ApiError, ErrorCode
from proskenion.config import Config
from proskenion.core import certs
from proskenion.core.certs import CertificateManager, IssuanceError, TokenError
from proskenion.core.cloudflare import CloudflareError

router = APIRouter(prefix="/system/certs", tags=["certs"])


class _Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")


def get_certs(request: Request) -> CertificateManager:
    manager: CertificateManager | None = getattr(request.app.state, "certs", None)
    if manager is None:
        raise ApiError(
            ErrorCode.INTERNAL_ERROR,
            "Certificate management is not running",
            {"reason": "not_started"},
        )
    return manager


def _card_json(info: certs.CertificateInfo | None) -> dict[str, Any] | None:
    if info is None:
        return None
    return {
        "domain": info.domain,
        "issuer": info.issuer,
        "issued": info.issued,
        "expires": info.expires,
        "days_remaining": info.days_remaining,
        "self_signed": info.self_signed,
        "expired": info.expired,
        "renewal_history": info.renewal_history,
    }


# -- the token (§3.2, §21.24 "Cloudflare DNS") ----------------------------------------


class TokenStateResponse(_Payload):
    """``GET /system/certs/token`` — write-only: whether one is set, never its value."""

    configured: bool


class TokenBody(_Payload):
    token: str


class TokenTestResponse(_Payload):
    ok: bool


@router.get("/token", response_model=TokenStateResponse, dependencies=[Depends(require_admin)])
async def get_token_state(
    manager: Annotated[CertificateManager, Depends(get_certs)],
) -> TokenStateResponse:
    return TokenStateResponse(configured=await manager.token_is_configured())


@router.put("/token", response_model=TokenStateResponse, dependencies=[Depends(require_admin)])
async def put_token(
    body: TokenBody, manager: Annotated[CertificateManager, Depends(get_certs)]
) -> TokenStateResponse:
    """Store the token, encrypted with the device secret (§3.2). Never echoed back."""
    if not body.token.strip():
        raise ApiError(
            ErrorCode.VALIDATION_FAILED,
            "A token is required",
            {"fields": [{"field": "token", "message": "Enter the API token.", "type": "required"}]},
        )
    await manager.set_token(body.token)
    return TokenStateResponse(configured=True)


@router.post(
    "/token/test", response_model=TokenTestResponse, dependencies=[Depends(require_admin)]
)
async def test_token(
    manager: Annotated[CertificateManager, Depends(get_certs)],
) -> TokenTestResponse:
    """A real zone read (§21.24's "Test"): proves the token authenticates and reaches the zone."""
    try:
        ok = await manager.test_token()
    except TokenError as exc:
        raise ApiError(ErrorCode.VALIDATION_FAILED, str(exc)) from exc
    except CloudflareError as exc:
        raise ApiError(ErrorCode.DEVICE_UNAVAILABLE, str(exc)) from exc
    return TokenTestResponse(ok=ok)


# -- issuance and history (§21.24's six steps, cert_issue) ----------------------------


class IssueBody(_Payload):
    hostname: str | None = None


class CertificateCardResponse(_Payload):
    certificate: dict[str, Any] | None


@router.post(
    "/issue", response_model=CertificateCardResponse, dependencies=[Depends(require_admin)]
)
async def issue(
    body: IssueBody, manager: Annotated[CertificateManager, Depends(get_certs)]
) -> CertificateCardResponse:
    """Issue or renew now. Progress arrives as ``progress`` frames (``cert_issue``, §6)."""
    try:
        info = await manager.issue(body.hostname, method="manual")
    except TokenError as exc:
        raise ApiError(ErrorCode.VALIDATION_FAILED, str(exc)) from exc
    except IssuanceError as exc:
        raise ApiError(ErrorCode.DEVICE_UNAVAILABLE, exc.detail) from exc
    return CertificateCardResponse(certificate=_card_json(info))


@router.post(
    "/self-signed",
    response_model=CertificateCardResponse,
    dependencies=[Depends(require_admin)],
)
async def use_self_signed(
    body: IssueBody, manager: Annotated[CertificateManager, Depends(get_certs)]
) -> CertificateCardResponse:
    """§21.24's "Use self-signed" (contracts §5, wave 3 additions): the way
    back when issuance cannot work — an expired token, no DNS, a site off
    the internet. Reaches neither Cloudflare nor an ACME server, so nothing
    here can fail the way ``/issue`` can; its real failures are the same
    "no hostname configured" ``/issue`` also reports as ``validation_failed``,
    and a hostname nginx does not actually serve, which would write a
    certificate nothing ever reads.
    """
    try:
        info = await manager.use_self_signed(body.hostname)
    except IssuanceError as exc:
        raise ApiError(ErrorCode.VALIDATION_FAILED, exc.detail) from exc
    return CertificateCardResponse(certificate=_card_json(info))


class HistoryResponse(_Payload):
    certificate: dict[str, Any] | None
    history: list[dict[str, Any]]


@router.get("/history", response_model=HistoryResponse, dependencies=[Depends(require_admin)])
async def history(manager: Annotated[CertificateManager, Depends(get_certs)]) -> HistoryResponse:
    """The §21.24 card plus renewal history, newest first."""
    hostname = manager.hostname
    info = None if not hostname else await certs.certificate_card(manager.data_dir, hostname)
    records = [] if not hostname else await certs.renewal_history(manager.data_dir, hostname)
    return HistoryResponse(
        certificate=_card_json(info),
        history=[r.to_json() for r in reversed(records)],
    )


# -- download (§6.16, public) ----------------------------------------------------------


@router.get("/download")
async def download(
    request: Request, config: Annotated[Config, Depends(get_config)]
) -> Response:
    """The served certificate, never the key (§6.16) — what a hirer's iPad is asked to trust.

    Deliberately public: a device deciding whether to trust this controller
    cannot first authenticate to it. ``manager`` is used when the application
    is fully wired; a bare ``config.server.hostname`` fallback keeps this
    working even where ``app.state.certs`` was never constructed (a minimal
    test app), since nothing about reading the served file needs the manager.
    """
    manager: CertificateManager | None = getattr(request.app.state, "certs", None)
    hostname = manager.hostname if manager is not None else config.server.hostname
    data_dir = manager.data_dir if manager is not None else config.app.data_dir
    if not hostname:
        raise ApiError(ErrorCode.NOT_FOUND, "No certificate is configured")
    paths = certs.certificate_paths(data_dir, hostname)
    info = await certs.describe(paths)
    if info is None:
        raise ApiError(ErrorCode.NOT_FOUND, "No certificate is installed")
    content = await asyncio.to_thread(paths.certificate.read_bytes)
    return Response(content=content, media_type="application/x-pem-file")
