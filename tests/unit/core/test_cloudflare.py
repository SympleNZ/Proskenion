"""``CloudflareClient`` against a fake transport (spec §3.2).

24 September 2026: the school's administrator pasted a Cloudflare token id
rather than the token itself. Cloudflare answered every request with
``success: false`` (errors ``6003 Invalid request headers``), and
``zone_id_for`` swallowed that into "no Cloudflare zone owns …" — which sent
the administrator looking at DNS and zone ownership for a token problem.
These tests prove the client now distinguishes "Cloudflare refused the
request" from "this suffix is not a zone" and raises the former with
Cloudflare's own code and message, from ``zone_id_for`` and ``verify_token``
alike; ``tests/unit/core/test_cert_manager.py`` and
``tests/integration/certs/`` cover the same client faked and for real,
respectively, so this file's job is the transport-level distinction only.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from proskenion.core.cloudflare import CloudflareClient, CloudflareError

HOSTNAME = "av.school.nz"
TOKEN = "cf-scoped-token"

Handler = Callable[[httpx.Request], httpx.Response]


def _client(handler: Handler) -> CloudflareClient:
    return CloudflareClient(TOKEN, transport=httpx.MockTransport(handler))


def _rejected(request: httpx.Request) -> httpx.Response:
    """Every request refused, exactly as Cloudflare answers a bad token."""
    return httpx.Response(
        200,
        json={
            "success": False,
            "result": None,
            "errors": [{"code": 6003, "message": "Invalid request headers"}],
        },
    )


# -- zone_id_for --------------------------------------------------------------------


def test_zone_id_for_returns_the_first_matching_zone() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["name"] == HOSTNAME
        return httpx.Response(200, json={"success": True, "result": [{"id": "zone-1"}]})

    with _client(handler) as client:
        assert client.zone_id_for(HOSTNAME) == "zone-1"


def test_zone_id_for_tries_a_narrower_suffix_when_a_successful_result_is_empty() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        name = request.url.params["name"]
        seen.append(name)
        if name == "school.nz":
            return httpx.Response(200, json={"success": True, "result": [{"id": "zone-2"}]})
        # The full name really is not a zone — a successful response, just empty.
        return httpx.Response(200, json={"success": True, "result": []})

    with _client(handler) as client:
        assert client.zone_id_for(HOSTNAME) == "zone-2"
    assert seen == [HOSTNAME, "school.nz"]


def test_zone_id_for_raises_with_cloudflares_code_and_message_on_a_rejected_token() -> None:
    """The defect: without the fix this raised "no Cloudflare zone owns …" instead,
    because a successful-empty-result and an unsuccessful response were treated
    alike. Every suffix here answers ``success: false`` — a rejected token, not
    an absent zone — so the very first attempt must raise, carrying Cloudflare's
    code and message, and must not be tried suffix-by-suffix until it gives up.
    """
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _rejected(request)

    with _client(handler) as client, pytest.raises(CloudflareError) as excinfo:
        client.zone_id_for(HOSTNAME)
    assert "6003" in str(excinfo.value)
    assert "Invalid request headers" in str(excinfo.value)
    assert "no Cloudflare zone owns" not in str(excinfo.value)
    assert calls == 1  # raised on the first suffix, never tried a second


# -- verify_token ---------------------------------------------------------------------


def test_verify_token_true_on_a_successful_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "result": [{"id": "zone-1"}]})

    with _client(handler) as client:
        assert client.verify_token() is True


def test_verify_token_raises_with_cloudflares_code_and_message_on_a_rejected_token() -> None:
    """The defect, at its root: a bad token used to make ``verify_token()``
    answer a bare ``False`` — indistinguishable from "this token is merely
    unscoped for zones" — instead of surfacing why Cloudflare refused it.
    """
    with _client(_rejected) as client, pytest.raises(CloudflareError) as excinfo:
        client.verify_token()
    assert "6003" in str(excinfo.value)
    assert "Invalid request headers" in str(excinfo.value)


# -- the other callers: already raised on refusal, but without the code -------------


def test_upsert_a_record_raises_with_cloudflares_code_and_message() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"success": True, "result": []})
        return _rejected(request)

    with _client(handler) as client, pytest.raises(CloudflareError) as excinfo:
        client.upsert_a_record("zone-1", HOSTNAME, "10.2.30.251")
    assert "6003" in str(excinfo.value)


def test_create_txt_record_raises_with_cloudflares_code_and_message() -> None:
    with _client(_rejected) as client, pytest.raises(CloudflareError) as excinfo:
        client.create_txt_record("zone-1", HOSTNAME, "validation-value")
    assert "6003" in str(excinfo.value)


def test_delete_txt_record_is_best_effort_and_never_raises() -> None:
    """Cleanup failure must not fail issuance (see the module docstring) — this
    stays true after the code/message fix, since it still only logs.
    """
    from proskenion.core.cloudflare import TxtRecord

    with _client(_rejected) as client:
        client.delete_txt_record(TxtRecord(record_id="rec-1", zone_id="zone-1", name="x"))
