"""Hashing, the signing secret, token issue and decode (§6.3–§6.5)."""

import os
import stat
from datetime import datetime, timedelta
from pathlib import Path

import bcrypt
import jwt
import pytest

from proskenion.core import auth
from proskenion.core.auth import TokenClaims, TokenError, TokenService, load_or_create_secret
from proskenion.db.connection import Database
from proskenion.db.crud import password_state as password_state_crud
from proskenion.db.crud import users as users_crud
from proskenion.db.crud.base import AUCKLAND
from proskenion.db.crud.users import PLACEHOLDER_HASH_PREFIX

START = datetime(2026, 9, 10, 18, 0, 0, tzinfo=AUCKLAND)


class Clock:
    def __init__(self) -> None:
        self.current = START

    def __call__(self) -> datetime:
        return self.current

    def advance(self, **kwargs: float) -> None:
        self.current += timedelta(**kwargs)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def tokens(tmp_path: Path, clock: Clock) -> TokenService:
    return TokenService(tmp_path / "state" / "jwt-secret", clock=clock)


# -- policy ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "issued",
    [
        # 20:00 on the Saturday before clocks go forward (02:00 → 03:00,
        # 27 September 2026): the 12 hours span the skipped hour.
        datetime(2026, 9, 26, 20, 0, 0, tzinfo=AUCKLAND),
        # 20:00 on the Saturday before clocks go back (03:00 → 02:00,
        # 4 April 2027): the 12 hours span the repeated hour.
        datetime(2027, 4, 3, 20, 0, 0, tzinfo=AUCKLAND),
    ],
    ids=["spring-forward", "fall-back"],
)
def test_the_absolute_cap_is_twelve_real_hours_across_a_daylight_saving_change(
    tmp_path: Path, issued: datetime
) -> None:
    service = TokenService(tmp_path / "jwt-secret", clock=lambda: issued)
    _, claims = service.issue("admin", 0)
    elapsed = claims.absolute_expires_at.timestamp() - claims.issued_at.timestamp()
    assert elapsed == auth.ABSOLUTE_HOURS * 3600
    idle = claims.expires_at.timestamp() - claims.issued_at.timestamp()
    assert idle == auth.IDLE_MINUTES * 60


def test_policy_constants() -> None:
    assert auth.IDLE_MINUTES == 30
    assert auth.ABSOLUTE_HOURS == 12
    assert auth.MIN_PASSWORD_LENGTH == 12
    assert auth.PIN_LENGTH == 6
    assert auth.BCRYPT_ROUNDS == 12
    assert auth.COOKIE_NAME == "proskenion_session"


# -- hashing --------------------------------------------------------------------


def test_hash_round_trip_uses_bcrypt_cost_12() -> None:
    hashed = auth.hash_secret("correct horse battery")
    assert hashed.startswith("$2b$12$")
    assert auth.verify_secret("correct horse battery", hashed)
    assert not auth.verify_secret("correct horse battery!", hashed)


async def test_async_wrappers() -> None:
    hashed = await auth.hash_secret_async("a-test-password", rounds=4)
    assert await auth.verify_secret_async("a-test-password", hashed)


def test_placeholder_never_matches_but_still_costs_a_bcrypt_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[bytes] = []
    real = bcrypt.checkpw

    def counting(password: bytes, hashed: bytes) -> bool:
        calls.append(hashed)
        return real(password, hashed)

    monkeypatch.setattr(auth.bcrypt, "checkpw", counting)
    placeholder = PLACEHOLDER_HASH_PREFIX + ".admin.replace.at.first.run..............."
    assert not auth.verify_secret("anything at all", placeholder)
    assert len(calls) == 1
    assert not calls[0].startswith(PLACEHOLDER_HASH_PREFIX.encode())


def test_malformed_hash_is_a_mismatch_not_an_error() -> None:
    assert not auth.verify_secret("anything", "not-a-hash")


# -- signing secret -------------------------------------------------------------


def test_secret_created_on_first_use_with_restricted_mode(tmp_path: Path) -> None:
    path = tmp_path / "appliance" / "jwt-secret"
    assert not path.exists()
    first = load_or_create_secret(path)
    assert len(first) == 32
    mode = stat.S_IMODE(os.stat(path).st_mode)
    if os.name == "posix":
        assert mode == 0o400
    else:
        assert not mode & 0o222  # Windows only models the write bit
    assert load_or_create_secret(path) == first


def test_short_secret_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "jwt-secret"
    path.write_bytes(b"short")
    with pytest.raises(RuntimeError, match="too short"):
        load_or_create_secret(path)


def test_token_service_reads_secret_lazily(tmp_path: Path, clock: Clock) -> None:
    path = tmp_path / "jwt-secret"
    service = TokenService(path, clock=clock)
    assert not path.exists()
    service.issue("admin", 0)
    assert path.exists()


# -- issue and decode -----------------------------------------------------------


def test_issue_staff_token_claims(tokens: TokenService, clock: Clock) -> None:
    token, claims = tokens.issue("admin", 3)
    assert claims == TokenClaims(
        tier="admin",
        token_version=3,
        issued_at=START,
        expires_at=START + timedelta(minutes=30),
        absolute_expires_at=START + timedelta(hours=12),
        session_id=None,
    )
    assert claims.is_staff and not claims.is_hirer
    payload = jwt.decode(
        token,
        tokens._key(),
        algorithms=["HS256"],
        options={"verify_exp": False, "verify_iat": False},
    )
    assert set(payload) == {"tier", "tv", "iat", "exp", "aexp"}
    assert tokens.decode(token) == claims


def test_hirer_token_carries_session_id_and_nothing_else(tokens: TokenService) -> None:
    token, claims = tokens.issue("hirer", 1)
    assert claims.is_hirer and claims.session_id
    payload = jwt.decode(
        token,
        tokens._key(),
        algorithms=["HS256"],
        options={"verify_exp": False, "verify_iat": False},
    )
    assert set(payload) == {"tier", "tv", "sid", "iat", "exp", "aexp"}
    assert tokens.decode(token) == claims
    _, again = tokens.issue("hirer", 1)
    assert again.session_id != claims.session_id


def test_staff_token_refuses_session_id_and_unknown_tier(tokens: TokenService) -> None:
    with pytest.raises(ValueError):
        tokens.issue("admin", 0, session_id="x")
    with pytest.raises(ValueError):
        tokens.issue("root", 0)


def test_reissue_refreshes_idle_and_keeps_absolute(tokens: TokenService, clock: Clock) -> None:
    _, first = tokens.issue("operator", 0)
    clock.advance(minutes=20)
    _, second = tokens.reissue(first)
    assert second.expires_at == START + timedelta(minutes=50)
    assert second.absolute_expires_at == first.absolute_expires_at
    assert second.token_version == 0
    _, bumped = tokens.reissue(first, token_version=4)
    assert bumped.token_version == 4


def test_idle_expiry_is_capped_by_absolute(tokens: TokenService, clock: Clock) -> None:
    _, first = tokens.issue("admin", 0)
    clock.advance(hours=11, minutes=50)
    _, later = tokens.reissue(first)
    assert later.expires_at == first.absolute_expires_at


def test_decode_rejects_idle_expiry(tokens: TokenService, clock: Clock) -> None:
    token, _ = tokens.issue("admin", 0)
    clock.advance(minutes=29, seconds=59)
    tokens.decode(token)
    clock.advance(seconds=1)
    with pytest.raises(TokenError) as exc:
        tokens.decode(token)
    assert exc.value.reason == "expired"


def test_decode_rejects_absolute_expiry_even_after_reissue(
    tokens: TokenService, clock: Clock
) -> None:
    token, claims = tokens.issue("hirer", 0)
    for _ in range(23):
        clock.advance(minutes=30)
        token, claims = tokens.reissue(claims)
    clock.advance(minutes=30)
    with pytest.raises(TokenError) as exc:
        tokens.decode(token)
    assert exc.value.reason == "expired"


def test_decode_rejects_tampering_and_foreign_secrets(
    tokens: TokenService, tmp_path: Path, clock: Clock
) -> None:
    token, _ = tokens.issue("admin", 0)
    head, body, sig = token.split(".")
    # Change a character in the middle of the signature: every bit of it is
    # significant, unlike the last base64url character, whose low bits are
    # padding, so rewriting the tail could leave the signature unchanged.
    mid = len(sig) // 2
    tampered = sig[:mid] + ("B" if sig[mid] == "A" else "A") + sig[mid + 1 :]
    with pytest.raises(TokenError) as exc:
        tokens.decode(f"{head}.{body}.{tampered}")
    assert exc.value.reason == "invalid"

    other = TokenService(tmp_path / "other-secret", clock=clock)
    foreign, _ = other.issue("admin", 0)
    with pytest.raises(TokenError):
        tokens.decode(foreign)

    with pytest.raises(TokenError):
        tokens.decode("garbage")


def test_decode_rejects_missing_or_bad_claims(tokens: TokenService) -> None:
    key = tokens._key()
    now = int(START.timestamp())
    base = {"iat": now, "exp": now + 60, "aexp": now + 3600}
    for payload in (
        {**base, "tier": "hirer", "tv": 0},  # hirer without sid
        {**base, "tier": "root", "tv": 0},  # unknown tier
        {**base, "tier": "admin"},  # no tv
        {**base, "tier": "admin", "tv": "x"},  # bad tv
    ):
        with pytest.raises(TokenError) as exc:
            tokens.decode(jwt.encode(payload, key, algorithm="HS256"))
        assert exc.value.reason == "invalid"


def test_none_algorithm_is_refused(tokens: TokenService) -> None:
    now = int(START.timestamp())
    payload = {"tier": "admin", "tv": 0, "iat": now, "exp": now + 60, "aexp": now + 60}
    unsigned = jwt.encode(payload, None, algorithm="none")  # type: ignore[arg-type]
    with pytest.raises(TokenError):
        tokens.decode(unsigned)


def test_iso_renders_auckland_offset() -> None:
    assert auth.iso(START) == "2026-09-10T18:00:00+12:00"


# -- set_staff_password (§21.23) -------------------------------------------------


async def test_set_staff_password_records_when_it_changed(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(auth, "BCRYPT_ROUNDS", 4)
    before = await users_crud.get_by_tier(db, "admin")
    assert before is not None and before.password_changed_at is None

    updated = await auth.set_staff_password(db, "admin", "a brand new password")
    assert updated.password_changed_at is not None
    assert updated.token_version == before.token_version + 1
    assert auth.verify_secret("a brand new password", updated.password)

    reread = await users_crud.get_by_tier(db, "admin")
    assert reread == updated


async def test_set_staff_password_marks_identical_when_the_tiers_now_match(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(auth, "BCRYPT_ROUNDS", 4)
    assert (await password_state_crud.get(db)).identical is False

    await auth.set_staff_password(db, "admin", "the-shared-password")
    # Only the admin side has changed so far — the operator still carries the
    # seed placeholder, which never counts as a match (§15.2).
    assert (await password_state_crud.get(db)).identical is False

    await auth.set_staff_password(db, "operator", "the-shared-password")
    assert (await password_state_crud.get(db)).identical is True

    # A further change that diverges again clears the flag — computed fresh
    # every time, never left stale from an earlier change.
    await auth.set_staff_password(db, "operator", "a-different-password-now")
    assert (await password_state_crud.get(db)).identical is False


async def test_set_staff_password_never_compares_two_hashes(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The identical flag is computed from the *plaintext* against the other
    tier's hash, not from two stored hashes — which would never agree even
    for equal passwords, since each bcrypt hash carries its own salt."""
    monkeypatch.setattr(auth, "BCRYPT_ROUNDS", 4)
    await auth.set_staff_password(db, "admin", "same-password-both")
    await auth.set_staff_password(db, "operator", "same-password-both")
    admin = await users_crud.get_by_tier(db, "admin")
    operator = await users_crud.get_by_tier(db, "operator")
    assert admin is not None and operator is not None
    assert admin.password != operator.password  # different salts, as bcrypt always gives
    assert (await password_state_crud.get(db)).identical is True
