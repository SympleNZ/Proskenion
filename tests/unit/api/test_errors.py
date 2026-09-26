"""The §16.1 error envelope on every failure path."""

import json
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient, Response

from proskenion.api.app import API_PREFIX
from proskenion.api.errors import DEFAULT_MESSAGE, HTTP_STATUS, ApiError, ErrorCode
from proskenion.api.request_id import REQUEST_ID_HEADER
from tests.unit.api.conftest import SECRET_EXCEPTION_TEXT

ENVELOPE_KEYS = {"code", "message", "detail", "request_id"}

EXPECTED_STATUS = {
    "unauthenticated": 401,
    "permission_denied": 403,
    "not_found": 404,
    "conflict": 409,
    "in_use": 409,
    "validation_failed": 422,
    "value_out_of_range": 422,
    "device_unavailable": 503,
    "rate_limited": 429,
    "internal_error": 500,
}


def read_log(log_file: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in log_file.read_text("utf-8").splitlines()]


def envelope(response: Response) -> dict[str, Any]:
    body = response.json()
    assert set(body) == {"error"}
    error: dict[str, Any] = body["error"]
    assert set(error) == ENVELOPE_KEYS
    assert error["request_id"] == response.headers[REQUEST_ID_HEADER]
    return error


def test_vocabulary_is_exactly_the_spec_table() -> None:
    assert {code.value for code in ErrorCode} == set(EXPECTED_STATUS)
    for code in ErrorCode:
        assert HTTP_STATUS[code] == EXPECTED_STATUS[code.value]
        assert DEFAULT_MESSAGE[code]


def test_api_error_rejects_codes_outside_the_vocabulary() -> None:
    with pytest.raises(TypeError):
        ApiError("permission_denied", "nope")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        ApiError("made_up_code")  # type: ignore[arg-type]


def test_api_error_defaults() -> None:
    error = ApiError(ErrorCode.NOT_FOUND)
    assert error.status == 404
    assert error.message == DEFAULT_MESSAGE[ErrorCode.NOT_FOUND]
    assert error.detail is None
    assert str(error) == f"not_found: {error.message}"


async def test_api_error_renders_envelope(client: AsyncClient) -> None:
    response = await client.get(f"{API_PREFIX}/probe/api-error")
    assert response.status_code == 403
    error = envelope(response)
    assert error["code"] == "permission_denied"
    assert error["message"] == "Channel 3 is not available to this session"
    assert error["detail"] == {"channel_id": 3}


async def test_api_error_headers_are_passed_through(client: AsyncClient) -> None:
    response = await client.get(f"{API_PREFIX}/probe/rate-limited")
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "30"
    error = envelope(response)
    assert error["code"] == "rate_limited"
    assert error["detail"] == {"retry_after": 30}


async def test_request_validation_error_has_field_detail(client: AsyncClient) -> None:
    response = await client.post(f"{API_PREFIX}/probe/validate", json={"level": "loud"})
    assert response.status_code == 422
    error = envelope(response)
    assert error["code"] == "validation_failed"
    fields = error["detail"]["fields"]
    by_field = {entry["field"]: entry for entry in fields}
    assert set(by_field) == {"body.level", "body.name"}
    assert by_field["body.name"]["type"] == "missing"
    assert by_field["body.level"]["message"]
    assert {"field", "message", "type"} <= set(by_field["body.level"])


async def test_malformed_json_body_is_validation_failed(client: AsyncClient) -> None:
    response = await client.post(
        f"{API_PREFIX}/probe/validate",
        content=b"{not json",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert envelope(response)["code"] == "validation_failed"


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, "unauthenticated"),
        (403, "permission_denied"),
        (404, "not_found"),
        (409, "conflict"),
        (422, "validation_failed"),
        (429, "rate_limited"),
        (503, "device_unavailable"),
    ],
)
async def test_http_exception_maps_to_vocabulary(
    client: AsyncClient, status: int, code: str
) -> None:
    response = await client.get(f"{API_PREFIX}/probe/http/{status}")
    assert response.status_code == status
    error = envelope(response)
    assert error["code"] == code
    assert error["message"] == f"http {status}"


async def test_http_exception_outside_vocabulary_is_internal_error(client: AsyncClient) -> None:
    response = await client.get(f"{API_PREFIX}/probe/http/418")
    assert response.status_code == 418
    assert envelope(response)["code"] == "internal_error"


async def test_unknown_route_is_not_found_envelope(client: AsyncClient) -> None:
    response = await client.get(f"{API_PREFIX}/no/such/route")
    assert response.status_code == 404
    assert envelope(response)["code"] == "not_found"


async def test_method_not_allowed_renders_envelope(client: AsyncClient) -> None:
    response = await client.delete("/health")
    assert response.status_code == 405
    error = envelope(response)
    assert error["code"] == "internal_error"


async def test_unhandled_exception(client: AsyncClient, log_file: Path) -> None:
    response = await client.get(f"{API_PREFIX}/probe/boom")
    assert response.status_code == 500
    error = envelope(response)
    assert error["code"] == "internal_error"
    assert error["detail"] is None
    assert SECRET_EXCEPTION_TEXT not in response.text

    request_id = error["request_id"]
    assert request_id
    lines = read_log(log_file)
    matching = [
        line for line in lines if line.get("request_id") == request_id and line["level"] == "ERROR"
    ]
    assert len(matching) == 1
    entry = matching[0]
    assert entry["logger"] == "proskenion.api.errors"
    assert "RuntimeError" in entry["message"]
    assert SECRET_EXCEPTION_TEXT in entry["exception"]
    assert "Traceback" in entry["exception"]


async def test_error_log_lines_carry_request_id(client: AsyncClient, log_file: Path) -> None:
    response = await client.get(f"{API_PREFIX}/probe/api-error")
    request_id = response.headers[REQUEST_ID_HEADER]
    lines = read_log(log_file)
    matching = [line for line in lines if line.get("request_id") == request_id]
    assert matching
    assert matching[0]["code"] == "permission_denied"
    assert matching[0]["status"] == 403
