"""The /auth endpoints (§16.3): one password field, sessions, invalidation, rate limits."""

import json
from collections.abc import Iterator
from datetime import datetime
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any

import bcrypt
import jwt
import pytest
from fastapi import FastAPI
from httpx import AsyncClient, Response

from proskenion.api.app import API_PREFIX
from proskenion.config import Config
from proskenion.core import auth
from proskenion.core.auth import COOKIE_NAME, TokenService
from proskenion.core.ratelimit import RateLimiter
from proskenion.db.connection import Database
from proskenion.db.crud import hirer as hirer_crud
from proskenion.db.crud import security_events
from proskenion.db.crud import users as users_crud
from tests.unit.api.conftest import (
    ADMIN_PASSWORD,
    HIRER_PIN,
    OPERATOR_PASSWORD,
    TEST_ROUNDS,
    FakeClock,
    build_app,
    make_client,
)

AUTH = f"{API_PREFIX}/auth"
WHOAMI = f"{API_PREFIX}/probe/whoami"
HIRER = f"{API_PREFIX}/hirer"


# -- helpers -------------------------------------------------------------------


def session_cookie(response: Response) -> tuple[str, str] | None:
    """(value, raw Set-Cookie header) for the session cookie, if the response set one."""
    for header in response.headers.get_list("set-cookie"):
        if header.startswith(f"{COOKIE_NAME}="):
            jar: SimpleCookie = SimpleCookie()
            jar.load(header)
            return jar[COOKIE_NAME].value, header
    return None


def raw_claims(tokens: TokenService, token: str) -> dict[str, Any]:
    payload: dict[str, Any] = jwt.decode(
        token,
        tokens._key(),
        algorithms=["HS256"],
        options={"verify_exp": False, "verify_iat": False},
    )
    return payload


async def login(client: AsyncClient, password: str = ADMIN_PASSWORD) -> Response:
    return await client.post(f"{AUTH}/login", json={"password": password})


async def hirer_login(client: AsyncClient, pin: str = HIRER_PIN) -> Response:
    return await client.post(f"{AUTH}/hirer", json={"pin": pin})


async def events(
    db: Database, event_type: str | None = None
) -> list[security_events.SecurityEvent]:
    return await db_events(db, event_type)


async def db_events(db: Database, event_type: str | None) -> list[security_events.SecurityEvent]:
    return await security_events.query(db, event_type=event_type)


def error(response: Response) -> dict[str, Any]:
    body: dict[str, Any] = response.json()["error"]
    return body


@pytest.fixture
def checkpw_calls(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[bytes]]:
    """Count every bcrypt verification the login path performs."""
    calls: list[bytes] = []
    real = bcrypt.checkpw

    def counting(password: bytes, hashed: bytes) -> bool:
        calls.append(hashed)
        return real(password, hashed)

    monkeypatch.setattr(auth.bcrypt, "checkpw", counting)
    yield calls


# -- staff login (§6.3) --------------------------------------------------------


async def test_login_admin_sets_cookie_and_returns_tier(client: AsyncClient) -> None:
    response = await login(client)
    assert response.status_code == 200
    body = response.json()
    assert body["tier"] == "admin"
    assert datetime.fromisoformat(body["expires_at"]).utcoffset() is not None
    assert set(body) == {"tier", "expires_at"}
    cookie = session_cookie(response)
    assert cookie is not None
    _, header = cookie
    lowered = header.lower()
    assert "httponly" in lowered
    assert "samesite=strict" in lowered
    assert "secure" in lowered
    assert "path=/" in lowered


async def test_login_operator(client: AsyncClient) -> None:
    response = await login(client, OPERATOR_PASSWORD)
    assert response.status_code == 200
    assert response.json()["tier"] == "operator"


async def test_admin_wins_when_both_match(client: AsyncClient, db: Database) -> None:
    shared = "the-same-password-for-both"
    await users_crud.set_password_hash(db, "admin", auth.hash_secret(shared, rounds=TEST_ROUNDS))
    await users_crud.set_password_hash(db, "operator", auth.hash_secret(shared, rounds=TEST_ROUNDS))
    response = await login(client, shared)
    assert response.status_code == 200
    assert response.json()["tier"] == "admin"


async def test_wrong_password_is_401_incorrect_password(client: AsyncClient) -> None:
    response = await login(client, "not-the-password")
    assert response.status_code == 401
    assert error(response)["code"] == "unauthenticated"
    assert error(response)["message"] == "Incorrect password"
    assert session_cookie(response) is None


async def test_both_hashes_verified_regardless_of_outcome(
    client: AsyncClient, checkpw_calls: list[bytes]
) -> None:
    await login(client, ADMIN_PASSWORD)
    right = len(checkpw_calls)
    checkpw_calls.clear()
    await login(client, "wrong-password-entirely")
    wrong = len(checkpw_calls)
    assert right == wrong == 2


async def test_placeholder_hashes_cost_the_same_and_never_match(
    client: AsyncClient, db: Database, checkpw_calls: list[bytes]
) -> None:
    placeholder = users_crud.PLACEHOLDER_HASH_PREFIX + "." * 41
    await users_crud.set_password_hash(db, "admin", placeholder)
    await users_crud.set_password_hash(db, "operator", placeholder)
    response = await login(client, ADMIN_PASSWORD)
    assert response.status_code == 401
    assert len(checkpw_calls) == 2
    assert all(not h.startswith(b"$2b$12$PLACEHOLDER") for h in checkpw_calls)


async def test_login_body_validation(client: AsyncClient) -> None:
    response = await client.post(f"{AUTH}/login", json={"username": "x", "password": "y"})
    assert response.status_code == 422
    assert error(response)["code"] == "validation_failed"


# -- session visibility (§6.4, §22.2) ------------------------------------------


async def test_ordinary_request_does_not_extend_session(
    client: AsyncClient, clock: FakeClock, tokens: TokenService
) -> None:
    cookie = session_cookie(await login(client))
    assert cookie is not None
    original = raw_claims(tokens, cookie[0])

    clock.advance(minutes=10)
    response = await client.get(WHOAMI)
    assert response.status_code == 200
    assert response.json()["tier"] == "admin"
    assert session_cookie(response) is None
    assert raw_claims(tokens, client.cookies[COOKIE_NAME])["exp"] == original["exp"]


async def test_get_session_reissues_with_fresh_idle_expiry(
    client: AsyncClient, clock: FakeClock, tokens: TokenService
) -> None:
    cookie = session_cookie(await login(client))
    assert cookie is not None
    original = raw_claims(tokens, cookie[0])

    clock.advance(minutes=10)
    response = await client.get(f"{AUTH}/session")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"tier", "expires_at", "absolute_expires_at", "server_time", "certificate"}
    assert body["tier"] == "admin"
    reissued = session_cookie(response)
    assert reissued is not None
    fresh = raw_claims(tokens, reissued[0])
    assert fresh["exp"] == original["exp"] + 10 * 60
    assert fresh["aexp"] == original["aexp"]
    assert body["expires_at"] == auth.iso(
        datetime.fromtimestamp(fresh["exp"], tz=clock.now().tzinfo)
    )
    assert body["server_time"] == auth.iso(clock.now())


async def test_session_expires_after_thirty_minutes_without_a_visible_page(
    client: AsyncClient, clock: FakeClock
) -> None:
    await login(client)
    clock.advance(minutes=29)
    assert (await client.get(WHOAMI)).status_code == 200
    clock.advance(minutes=2)
    response = await client.get(WHOAMI)
    assert response.status_code == 401
    assert error(response)["detail"]["reason"] == "expired"


async def test_get_session_never_extends_past_absolute_cap(
    client: AsyncClient, clock: FakeClock, tokens: TokenService
) -> None:
    cookie = session_cookie(await login(client))
    assert cookie is not None
    aexp = raw_claims(tokens, cookie[0])["aexp"]

    # A visible page polls every five minutes; within the cap each re-issue
    # is a full idle window, and the final one is truncated to the cap.
    for _ in range((11 * 60 + 40) // 5):
        clock.advance(minutes=5)
        polled = await client.get(f"{AUTH}/session")
        assert polled.status_code == 200
        reissued = session_cookie(polled)
        assert reissued is not None
        assert raw_claims(tokens, reissued[0])["exp"] <= aexp
        assert raw_claims(tokens, reissued[0])["aexp"] == aexp
    clock.advance(minutes=5)  # 11 h 45 min in
    response = await client.get(f"{AUTH}/session")
    assert response.status_code == 200
    reissued = session_cookie(response)
    assert reissued is not None
    assert raw_claims(tokens, reissued[0])["exp"] == aexp
    assert response.json()["expires_at"] == response.json()["absolute_expires_at"]


@pytest.mark.parametrize("tier", ["admin", "operator", "hirer"])
async def test_absolute_cap_applies_to_every_tier(
    client: AsyncClient, clock: FakeClock, db: Database, tier: str
) -> None:
    await hirer_crud.set_enabled(db, True, updated_by=None)
    if tier == "hirer":
        response = await hirer_login(client)
    else:
        response = await login(client, ADMIN_PASSWORD if tier == "admin" else OPERATOR_PASSWORD)
    assert response.status_code == 200 and response.json()["tier"] == tier

    # A visible page polls every five minutes for the whole event.
    for _ in range(12 * 12 - 1):
        clock.advance(minutes=5)
        assert (await client.get(f"{AUTH}/session")).status_code == 200
    clock.advance(minutes=5)
    response = await client.get(f"{AUTH}/session")
    assert response.status_code == 401
    assert error(response)["detail"]["reason"] == "expired"


async def test_missing_cookie_is_401_missing(client: AsyncClient) -> None:
    response = await client.get(WHOAMI)
    assert response.status_code == 401
    assert error(response)["code"] == "unauthenticated"
    assert error(response)["detail"]["reason"] == "missing"


async def test_garbage_cookie_is_401_invalid(client: AsyncClient) -> None:
    client.cookies.set(COOKIE_NAME, "not.a.token")
    response = await client.get(WHOAMI)
    assert response.status_code == 401
    assert error(response)["detail"]["reason"] == "invalid"


# -- logout ---------------------------------------------------------------------


async def test_logout_clears_cookie(client: AsyncClient) -> None:
    await login(client)
    response = await client.post(f"{AUTH}/logout")
    assert response.status_code == 204
    header = response.headers["set-cookie"].lower()
    assert header.startswith(f"{COOKIE_NAME}=")
    assert "max-age=0" in header
    assert (await client.get(WHOAMI)).status_code == 401


# -- change password and token invalidation (§16.3, §22.4) ----------------------


async def test_change_password_invalidates_old_token_and_reissues_caller(
    client: AsyncClient, tokens: TokenService, db: Database
) -> None:
    old = session_cookie(await login(client))
    assert old is not None
    before = await users_crud.get_by_tier(db, "admin")
    assert before is not None

    response = await client.post(
        f"{AUTH}/change-password",
        json={"current_password": ADMIN_PASSWORD, "new_password": "a-brand-new-password"},
    )
    assert response.status_code == 200
    assert response.json()["tier"] == "admin"
    reissued = session_cookie(response)
    assert reissued is not None

    after = await users_crud.get_by_tier(db, "admin")
    assert after is not None
    assert after.token_version == before.token_version + 1
    assert raw_claims(tokens, reissued[0])["tv"] == after.token_version
    assert raw_claims(tokens, reissued[0])["aexp"] == raw_claims(tokens, old[0])["aexp"]

    # The re-issued cookie works; the old one is dead.
    assert (await client.get(WHOAMI)).status_code == 200
    client.cookies.set(COOKIE_NAME, old[0])
    stale = await client.get(WHOAMI)
    assert stale.status_code == 401
    assert error(stale)["detail"]["reason"] == "revoked"

    # The new password signs in; the old one does not.
    client.cookies.clear()
    assert (await login(client, "a-brand-new-password")).status_code == 200
    assert (await login(client, ADMIN_PASSWORD)).status_code == 401

    changed = await events(db, "password_changed")
    assert len(changed) == 1
    assert changed[0].user_ident == "admin"


async def test_change_password_wrong_current_is_validation_failed(client: AsyncClient) -> None:
    await login(client)
    response = await client.post(
        f"{AUTH}/change-password",
        json={"current_password": "nope-nope-nope", "new_password": "a-brand-new-password"},
    )
    assert response.status_code == 422
    assert error(response)["code"] == "validation_failed"
    assert error(response)["detail"]["fields"][0]["field"] == "current_password"
    # Still signed in.
    assert (await client.get(WHOAMI)).status_code == 200


async def test_change_password_enforces_minimum_length(client: AsyncClient) -> None:
    await login(client)
    response = await client.post(
        f"{AUTH}/change-password",
        json={"current_password": ADMIN_PASSWORD, "new_password": "short"},
    )
    assert response.status_code == 422
    assert error(response)["code"] == "validation_failed"
    fields = {f["field"] for f in error(response)["detail"]["fields"]}
    assert "body.new_password" in fields


async def test_change_password_requires_staff(client: AsyncClient, db: Database) -> None:
    await hirer_crud.set_enabled(db, True, updated_by=None)
    await hirer_login(client)
    response = await client.post(
        f"{AUTH}/change-password",
        json={"current_password": HIRER_PIN, "new_password": "a-brand-new-password"},
    )
    assert response.status_code == 403
    assert error(response)["code"] == "permission_denied"


async def test_operator_change_password_only_affects_operator(
    client: AsyncClient, db: Database
) -> None:
    await login(client, OPERATOR_PASSWORD)
    admin_before = await users_crud.get_by_tier(db, "admin")
    response = await client.post(
        f"{AUTH}/change-password",
        json={"current_password": OPERATOR_PASSWORD, "new_password": "operator-new-password"},
    )
    assert response.status_code == 200 and response.json()["tier"] == "operator"
    admin_after = await users_crud.get_by_tier(db, "admin")
    assert admin_before == admin_after


# -- an admin resets the operator's password (§21.23) ----------------------------


async def test_admin_resets_operator_password_with_the_admins_own_password(
    app: FastAPI, client: AsyncClient, tokens: TokenService, db: Database
) -> None:
    await login(client)  # admin

    async with make_client(app) as operator_client:
        old_operator_cookie = session_cookie(await login(operator_client, OPERATOR_PASSWORD))
        assert old_operator_cookie is not None

        response = await client.post(
            f"{AUTH}/operator-password",
            json={"current_password": ADMIN_PASSWORD, "new_password": "operator-reset-by-admin"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["tier"] == "operator"
        assert body["password_changed_at"] is not None

        # The admin's own session survives untouched.
        assert (await client.get(WHOAMI)).status_code == 200

        # The operator's previous session is dead.
        operator_client.cookies.set(COOKIE_NAME, old_operator_cookie[0])
        stale = await operator_client.get(WHOAMI)
        assert stale.status_code == 401
        assert error(stale)["detail"]["reason"] == "revoked"

        # The new password signs in; the old one does not.
        operator_client.cookies.clear()
        assert (await login(operator_client, "operator-reset-by-admin")).status_code == 200
        assert (await login(operator_client, OPERATOR_PASSWORD)).status_code == 401

    changed = await events(db, "password_changed")
    assert len(changed) == 1
    assert changed[0].user_ident == "operator"
    assert json.loads(changed[0].detail or "{}")["changed_by"] == "admin"


async def test_admin_resets_operator_password_wrong_admin_password_is_validation_failed(
    client: AsyncClient, db: Database
) -> None:
    await login(client)
    before = await users_crud.get_by_tier(db, "operator")
    response = await client.post(
        f"{AUTH}/operator-password",
        json={
            "current_password": "not-the-admins-password",
            "new_password": "operator-new-password",
        },
    )
    assert response.status_code == 422
    assert error(response)["code"] == "validation_failed"
    assert error(response)["detail"]["fields"][0]["field"] == "current_password"
    after = await users_crud.get_by_tier(db, "operator")
    assert before == after  # nothing changed


async def test_admin_resets_operator_password_requires_admin_tier(client: AsyncClient) -> None:
    await login(client, OPERATOR_PASSWORD)
    response = await client.post(
        f"{AUTH}/operator-password",
        json={"current_password": OPERATOR_PASSWORD, "new_password": "operator-new-password"},
    )
    assert response.status_code == 403
    assert error(response)["code"] == "permission_denied"


# -- password status (§21.23) -----------------------------------------------------


async def test_password_status_reports_changed_at_and_identical(
    client: AsyncClient, db: Database
) -> None:
    await login(client)
    initial = await client.get(f"{AUTH}/password-status")
    assert initial.status_code == 200, initial.text
    body = initial.json()
    # The test harness seeds real passwords directly (conftest.py's `db`
    # fixture), so both are already recorded here — unlike a fresh appliance,
    # where they would be `None` ("Not recorded", §21.23) until the seed
    # placeholder is first replaced.
    admin_changed_at_before = body["admin"]["password_changed_at"]
    assert admin_changed_at_before is not None
    assert body["operator"]["password_changed_at"] is not None
    assert body["identical"] is False

    await client.post(
        f"{AUTH}/change-password",
        json={"current_password": ADMIN_PASSWORD, "new_password": "shared-password-both"},
    )
    # The caller's own session was reissued by the change above.
    response = await client.get(f"{AUTH}/password-status")
    assert response.status_code == 200
    body = response.json()
    assert body["admin"]["password_changed_at"] != admin_changed_at_before
    assert body["identical"] is False

    await client.post(
        f"{AUTH}/operator-password",
        json={"current_password": "shared-password-both", "new_password": "shared-password-both"},
    )
    response = await client.get(f"{AUTH}/password-status")
    body = response.json()
    assert body["operator"]["password_changed_at"] is not None
    assert body["identical"] is True


async def test_password_status_requires_admin_tier(client: AsyncClient) -> None:
    await login(client, OPERATOR_PASSWORD)
    response = await client.get(f"{AUTH}/password-status")
    assert response.status_code == 403
    assert error(response)["code"] == "permission_denied"


# -- hirer (§6.2, §6.4, B31) ----------------------------------------------------


async def test_hirer_disabled_is_403_with_exact_message(client: AsyncClient) -> None:
    response = await hirer_login(client)
    assert response.status_code == 403
    assert error(response)["code"] == "permission_denied"
    assert error(response)["detail"]["reason"] == "hirer_disabled"
    assert (
        error(response)["message"]
        == "Hire guest access is not currently available. Please contact venue staff."
    )


async def test_hirer_login_issues_identity_only_token(
    client: AsyncClient, db: Database, tokens: TokenService
) -> None:
    await hirer_crud.set_enabled(db, True, updated_by=None)
    response = await hirer_login(client)
    assert response.status_code == 200
    assert response.json()["tier"] == "hirer"
    cookie = session_cookie(response)
    assert cookie is not None
    payload = raw_claims(tokens, cookie[0])
    assert set(payload) == {"tier", "tv", "sid", "iat", "exp", "aexp"}
    assert payload["tier"] == "hirer"
    assert isinstance(payload["sid"], str) and payload["sid"]
    whoami = await client.get(WHOAMI)
    assert whoami.json() == {"tier": "hirer", "session_id": payload["sid"]}


async def test_wrong_pin_is_401(client: AsyncClient, db: Database) -> None:
    await hirer_crud.set_enabled(db, True, updated_by=None)
    response = await hirer_login(client, "000000")
    assert response.status_code == 401
    assert error(response)["code"] == "unauthenticated"


@pytest.mark.parametrize("pin", ["12345", "1234567", "12a456", "", "١٢٣٤٥٦"])
async def test_pin_must_be_exactly_six_ascii_digits(
    client: AsyncClient, db: Database, pin: str
) -> None:
    await hirer_crud.set_enabled(db, True, updated_by=None)
    response = await hirer_login(client, pin)
    assert response.status_code == 422
    assert error(response)["code"] == "validation_failed"


async def test_pin_change_revokes_hirer_token(app: FastAPI, client: AsyncClient) -> None:
    async with make_client(app) as admin:
        assert (await login(admin)).status_code == 200
        assert (await admin.post(f"{HIRER}/enabled", json={"enabled": True})).status_code == 200
        await hirer_login(client)
        assert (await client.get(WHOAMI)).status_code == 200
        changed = await admin.post(f"{HIRER}/pin", json={"pin": "999999"})
        assert changed.status_code == 200
    response = await client.get(WHOAMI)
    assert response.status_code == 401
    assert error(response)["code"] == "unauthenticated"
    assert error(response)["detail"]["reason"] == "hirer_revoked"


async def test_disabling_access_revokes_hirer_token(app: FastAPI, client: AsyncClient) -> None:
    async with make_client(app) as admin:
        assert (await login(admin)).status_code == 200
        assert (await admin.post(f"{HIRER}/enabled", json={"enabled": True})).status_code == 200
        await hirer_login(client)
        assert (await admin.post(f"{HIRER}/enabled", json={"enabled": False})).status_code == 200
        response = await client.get(WHOAMI)
        assert response.status_code == 401
        assert error(response)["detail"]["reason"] == "hirer_revoked"
        # Re-enabling does not resurrect the old token (the version was bumped).
        assert (await admin.post(f"{HIRER}/enabled", json={"enabled": True})).status_code == 200
    assert (await client.get(WHOAMI)).status_code == 401


# -- rate limiting (§6.8) -------------------------------------------------------


def from_ip(ip: str) -> dict[str, str]:
    return {"X-Forwarded-For": ip}


async def test_sixth_staff_attempt_in_five_minutes_is_rate_limited(
    client: AsyncClient, clock: FakeClock, db: Database
) -> None:
    for _ in range(5):
        response = await client.post(
            f"{AUTH}/login", json={"password": "wrong"}, headers=from_ip("10.0.0.7")
        )
        assert response.status_code == 401
        clock.advance(seconds=30)

    response = await client.post(
        f"{AUTH}/login", json={"password": ADMIN_PASSWORD}, headers=from_ip("10.0.0.7")
    )
    assert response.status_code == 429
    assert error(response)["code"] == "rate_limited"
    retry_after = error(response)["detail"]["retry_after"]
    assert 0 < retry_after <= 15 * 60
    assert response.headers["Retry-After"] == str(retry_after)

    # Another address is unaffected.
    other = await client.post(
        f"{AUTH}/login", json={"password": ADMIN_PASSWORD}, headers=from_ip("10.0.0.8")
    )
    assert other.status_code == 200

    # The lockout lifts after fifteen minutes.
    clock.advance(minutes=15)
    lifted = await client.post(
        f"{AUTH}/login", json={"password": ADMIN_PASSWORD}, headers=from_ip("10.0.0.7")
    )
    assert lifted.status_code == 200

    lockouts = await events(db, "lockout")
    assert len(lockouts) == 1
    assert lockouts[0].ip_address == "10.0.0.7"
    assert json.loads(lockouts[0].detail or "{}")["scope"] == "staff_login"


async def test_fourth_hirer_attempt_in_ten_minutes_locks_for_thirty(
    client: AsyncClient, clock: FakeClock, db: Database
) -> None:
    await hirer_crud.set_enabled(db, True, updated_by=None)
    for _ in range(3):
        response = await client.post(
            f"{AUTH}/hirer", json={"pin": "000000"}, headers=from_ip("10.0.0.9")
        )
        assert response.status_code == 401
    response = await client.post(
        f"{AUTH}/hirer", json={"pin": HIRER_PIN}, headers=from_ip("10.0.0.9")
    )
    assert response.status_code == 429
    assert error(response)["detail"]["retry_after"] == 30 * 60
    clock.advance(minutes=29)
    assert (
        await client.post(f"{AUTH}/hirer", json={"pin": HIRER_PIN}, headers=from_ip("10.0.0.9"))
    ).status_code == 429
    clock.advance(minutes=1)
    assert (
        await client.post(f"{AUTH}/hirer", json={"pin": HIRER_PIN}, headers=from_ip("10.0.0.9"))
    ).status_code == 200


async def test_success_clears_the_counter(client: AsyncClient, clock: FakeClock) -> None:
    for _ in range(4):
        await client.post(f"{AUTH}/login", json={"password": "wrong"}, headers=from_ip("10.0.0.7"))
    assert (await login(client)).status_code == 200  # from 127.0.0.1
    ok = await client.post(
        f"{AUTH}/login", json={"password": ADMIN_PASSWORD}, headers=from_ip("10.0.0.7")
    )
    assert ok.status_code == 200
    client.cookies.clear()
    for _ in range(4):
        response = await client.post(
            f"{AUTH}/login", json={"password": "wrong"}, headers=from_ip("10.0.0.7")
        )
        assert response.status_code == 401  # not locked: the success reset the count


async def test_hirer_disabled_attempts_do_not_count_against_the_limit(
    client: AsyncClient,
) -> None:
    for _ in range(5):
        response = await hirer_login(client)
        assert response.status_code == 403


# -- security events (§6.14) ----------------------------------------------------


async def test_events_record_each_type_with_the_real_address(
    client: AsyncClient, db: Database
) -> None:
    await login(client)  # login_success from the loopback peer (no proxy header)
    await client.post(f"{AUTH}/login", json={"password": "wrong"}, headers=from_ip("10.1.1.1"))
    await client.post(
        f"{AUTH}/change-password",
        json={"current_password": ADMIN_PASSWORD, "new_password": "a-brand-new-password"},
        headers=from_ip("10.1.1.2"),
    )
    successes = await events(db, "login_success")
    failures = await events(db, "login_failure")
    changed = await events(db, "password_changed")
    assert [(e.user_ident, e.ip_address) for e in successes] == [("admin", "127.0.0.1")]
    assert [(e.user_ident, e.ip_address) for e in failures] == [("10.1.1.1", "10.1.1.1")]
    assert [(e.user_ident, e.ip_address) for e in changed] == [("admin", "10.1.1.2")]
    for event in successes + failures + changed:
        assert datetime.fromisoformat(event.timestamp).utcoffset() is not None
        assert isinstance(json.loads(event.detail or "{}"), dict)


async def test_permission_denied_is_403_and_recorded(client: AsyncClient, db: Database) -> None:
    await login(client, OPERATOR_PASSWORD)
    response = await client.get(f"{API_PREFIX}/probe/admin-only", headers=from_ip("10.2.2.2"))
    assert response.status_code == 403
    assert error(response)["code"] == "permission_denied"
    denied = await events(db, "permission_denied")
    assert len(denied) == 1
    assert (denied[0].user_ident, denied[0].ip_address) == ("operator", "10.2.2.2")
    assert json.loads(denied[0].detail or "{}")["required"] == ["admin"]


async def test_admin_passes_admin_gate(client: AsyncClient) -> None:
    await login(client)
    response = await client.get(f"{API_PREFIX}/probe/admin-only")
    assert response.status_code == 200 and response.json() == {"tier": "admin"}


async def test_unexpected_origin_is_served_and_logged(
    hostname_config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    app: FastAPI = build_app(hostname_config, db, tokens, limiter)
    async with make_client(app) as client:
        good = await client.get("/health", headers={"Origin": "https://AV.school.nz"})
        bad = await client.get(
            "/health", headers={"Origin": "https://evil.example", **from_ip("10.3.3.3")}
        )
    assert good.status_code == 200 and bad.status_code == 200
    logged = await events(db, "unexpected_origin")
    assert len(logged) == 1
    assert logged[0].ip_address == "10.3.3.3"
    assert json.loads(logged[0].detail or "{}")["origin"] == "https://evil.example"


async def test_origin_check_skipped_without_configured_hostname(
    client: AsyncClient, db: Database
) -> None:
    await client.get("/health", headers={"Origin": "https://evil.example"})
    assert await events(db, "unexpected_origin") == []


# -- cookie flags under development ---------------------------------------------


async def test_development_config_drops_secure_flag(
    dev_config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    app = build_app(dev_config, db, tokens, limiter)
    async with make_client(app) as client:
        response = await login(client)
    cookie = session_cookie(response)
    assert cookie is not None
    lowered = cookie[1].lower()
    assert "secure" not in lowered
    assert "httponly" in lowered and "samesite=strict" in lowered


# -- §22.4: logout has no failure mode, deliberately ---------------------------


async def test_logout_without_a_session_is_still_accepted(client: AsyncClient) -> None:
    """§16.3: the cookie is cleared whether or not the token is still valid.

    Recorded because §22.4 asks for each endpoint's primary failure mode: this
    one has none by design. A tablet whose session expired while it was asleep
    must still be able to sign out, and refusing it would leave the cookie in
    place — the opposite of what the caller asked for.
    """
    response = await client.post(f"{AUTH}/logout")
    assert response.status_code == 204
    assert "max-age=0" in response.headers["set-cookie"].lower()


# -- §6.16: the served certificate's trust, without a configured hostname -----------
#
# §4.14's bootstrap config on the appliance carries no [server] hostname, so
# config.server.hostname is None there. That once made GET
# /auth/session's "certificate" field always answer self_signed regardless of
# what nginx actually serves — the install prompt (§21.8) would stay
# suppressed on iOS even once a real, trusted certificate was installed.


async def test_session_certificate_trust_is_read_from_the_served_name(
    config: Config,
    db: Database,
    tokens: TokenService,
    limiter: RateLimiter,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from proskenion.core import certs
    from proskenion.core.platform import DevelopmentPlatform
    from tests.unit.core.test_cert_manager import _issued_certificate

    assert config.server.hostname is None  # exactly the appliance's §4.14 shape

    site = tmp_path / "auditorium.conf"
    site.write_text(
        "    ssl_certificate     /data/certs/live/auditorium.obhs.school.nz/fullchain.pem;\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(certs, "NGINX_SITE_CONFIG", site)

    data_dir = tmp_path / "data"
    # A real (issuer != subject) certificate — describe_certificate reads
    # self_signed straight off that, the same signal served_trust uses.
    issued = _issued_certificate("auditorium.obhs.school.nz")
    certs.write_certificate_pair(
        data_dir, "auditorium.obhs.school.nz", issued.key_pem, issued.fullchain_pem
    )

    app = build_app(config, db, tokens, limiter)
    # No app.state.certs: proves the served_hostnames() fallback itself, not
    # just a running CertificateManager's already-resolved .hostname.
    app.state.platform = DevelopmentPlatform(data_dir=data_dir)

    async with make_client(app) as client:
        signed_in = await login(client)
        assert signed_in.status_code == 200
        response = await client.get(f"{AUTH}/session")

    assert response.status_code == 200
    assert response.json()["certificate"] == "trusted"


