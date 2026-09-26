"""Credentials and session tokens (spec §6.3–§6.5, §6.14).

Passwords and the hirer PIN are bcrypt hashes (§6.4). Verification is
constant-work: a seed placeholder (§15.2) is never handed to bcrypt, but a
dummy hash is checked in its place so a placeholder costs the same as a real
mismatch, and the login handler always verifies *both* staff hashes (§6.3).

Sessions are JSON Web Tokens (HS256) signed with a per-installation secret:
32 random bytes at ``<state_dir>/jwt-secret``, created on first use with mode
0400, alongside the device secret (§2.3). Claims carry identity only — the
tier, the ``token_version`` that invalidates them, and for hirers a session
id — never permissions (§6.4, B31):

======  ==============================================================
claim   meaning
======  ==============================================================
tier    ``admin`` | ``operator`` | ``hirer``
tv      the account's ``token_version`` when the token was issued
sid     hirer session id (hirers only)
iat     issued at (Unix seconds)
exp     idle expiry — 30 minutes from issue or re-issue
aexp    absolute expiry — 12 hours from the *first* issue, carried forward
======  ==============================================================

Foreground presence holds a session, not traffic (§6.4, B65): the only thing
that re-issues a token is ``GET /auth/session``, which the browser calls while
the page is visible. ``exp`` never passes ``aexp``.

Times are handled here as aware :class:`datetime` values and rendered as
ISO 8601 with the Pacific/Auckland offset (§4.9). The clock is injectable so
expiry is testable without sleeping.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final, Literal, Protocol

import bcrypt
import jwt

from proskenion.core.elapsed import elapsed_after
from proskenion.db.connection import Database
from proskenion.db.crud import password_state as password_state_crud
from proskenion.db.crud import security_events
from proskenion.db.crud import users as users_crud
from proskenion.db.crud.base import AUCKLAND
from proskenion.db.crud.users import is_placeholder_hash

log = logging.getLogger(__name__)

# -- policy (§6.4) --------------------------------------------------------------

IDLE_MINUTES: Final = 30
ABSOLUTE_HOURS: Final = 12
MIN_PASSWORD_LENGTH: Final = 12
#: Exactly six digits: the sign-in page has six boxes (§6.2, §21.8).
PIN_LENGTH: Final = 6

BCRYPT_ROUNDS: Final = 12

COOKIE_NAME: Final = "proskenion_session"
JWT_ALGORITHM: Final = "HS256"
JWT_SECRET_FILENAME: Final = "jwt-secret"
JWT_SECRET_BYTES: Final = 32
DEFAULT_JWT_SECRET_PATH: Final = Path("/srv/appliance") / JWT_SECRET_FILENAME

Tier = Literal["admin", "operator", "hirer"]
STAFF_TIERS: Final[frozenset[str]] = frozenset({"admin", "operator"})
ALL_TIERS: Final[frozenset[str]] = frozenset({"admin", "operator", "hirer"})

Clock = Callable[[], datetime]
"""Returns the current time as an aware datetime."""


def now_auckland() -> datetime:
    return datetime.now(tz=AUCKLAND)


def iso(moment: datetime) -> str:
    """ISO 8601 with offset in Pacific/Auckland, whole seconds (§4.9)."""
    return moment.astimezone(AUCKLAND).isoformat(timespec="seconds")


# -- password hashing -----------------------------------------------------------


def hash_secret(secret: str, *, rounds: int = BCRYPT_ROUNDS) -> str:
    """bcrypt hash of a password or PIN. Blocking — call via :func:`hash_secret_async`."""
    return bcrypt.hashpw(secret.encode("utf-8"), bcrypt.gensalt(rounds)).decode("ascii")


async def hash_secret_async(secret: str, *, rounds: int = BCRYPT_ROUNDS) -> str:
    return await asyncio.to_thread(hash_secret, secret, rounds=rounds)


_dummy_hash: str | None = None


def _dummy() -> str:
    """A real bcrypt hash of a random value, checked in place of a placeholder."""
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = hash_secret(secrets.token_urlsafe(24))
    return _dummy_hash


def verify_secret(secret: str, stored_hash: str) -> bool:
    """Constant-work check of ``secret`` against a stored bcrypt hash.

    A seed placeholder (§15.2) is refused, but bcrypt still runs against a
    dummy hash so the placeholder and a real mismatch take the same time.
    Blocking — call via :func:`verify_secret_async`.
    """
    if is_placeholder_hash(stored_hash):
        bcrypt.checkpw(secret.encode("utf-8"), _dummy().encode("ascii"))
        return False
    try:
        return bcrypt.checkpw(secret.encode("utf-8"), stored_hash.encode("ascii"))
    except ValueError:
        # Not a bcrypt hash at all. Treat like a placeholder: same work, no match.
        bcrypt.checkpw(secret.encode("utf-8"), _dummy().encode("ascii"))
        return False


async def verify_secret_async(secret: str, stored_hash: str) -> bool:
    return await asyncio.to_thread(verify_secret, secret, stored_hash)


# -- staff password changes (§21.23) ---------------------------------------------


async def set_staff_password(db: Database, tier: str, new_password: str) -> users_crud.User:
    """Hash, store and bump ``token_version`` for ``tier``'s password.

    The single choke point every change path goes through: the API
    (``POST /auth/change-password``, ``POST /auth/operator-password``), the
    first-run wizard's steps 2 and 5, and ``avc-reset-password``. Also
    recomputes the identical-passwords flag (§21.23's informational note) —
    the new plaintext, which is only ever available here, is checked against
    the *other* tier's stored hash rather than the two hashes being compared
    to each other, which would never agree even for equal passwords.

    ``BCRYPT_ROUNDS`` is looked up as a module global rather than a default
    argument, so a test that lowers it with ``monkeypatch.setattr(auth,
    "BCRYPT_ROUNDS", ...)`` is honoured here too.
    """
    other_tier = "operator" if tier == "admin" else "admin"
    new_hash = await hash_secret_async(new_password, rounds=BCRYPT_ROUNDS)
    other = await users_crud.get_by_tier(db, other_tier)
    identical = other is not None and await verify_secret_async(new_password, other.password)
    updated = await users_crud.set_password_hash(db, tier, new_hash)
    await password_state_crud.set_identical(db, identical)
    return updated


# -- signing secret -------------------------------------------------------------


def load_or_create_secret(path: Path) -> bytes:
    """Read the signing secret, creating it (32 random bytes, mode 0400) if absent."""
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        pass
    else:
        if len(data) < JWT_SECRET_BYTES:
            raise RuntimeError(f"{path}: JWT secret is too short ({len(data)} bytes)")
        return data

    path.parent.mkdir(parents=True, exist_ok=True)
    data = secrets.token_bytes(JWT_SECRET_BYTES)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
    except FileExistsError:
        # Another process won the race; theirs is as good as ours.
        return load_or_create_secret(path)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    os.chmod(path, 0o400)
    log.info("created JWT signing secret at %s", path)
    return data


# -- tokens ---------------------------------------------------------------------


class TokenError(Exception):
    """The token cannot be accepted. ``reason`` is the machine-readable cause."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


@dataclass(frozen=True, slots=True)
class TokenClaims:
    """The decoded identity a request carries. Never permissions (B31)."""

    tier: str
    token_version: int
    issued_at: datetime
    expires_at: datetime
    absolute_expires_at: datetime
    session_id: str | None = None  # hirers only

    @property
    def is_staff(self) -> bool:
        return self.tier in STAFF_TIERS

    @property
    def is_hirer(self) -> bool:
        return self.tier == "hirer"


class TokenService:
    """Issue and decode session tokens with the installation's secret.

    The secret file is read (or created) on first use, not at construction, so
    building the application never touches the state partition.
    """

    def __init__(
        self,
        secret_path: Path = DEFAULT_JWT_SECRET_PATH,
        *,
        clock: Clock = now_auckland,
        idle: timedelta = timedelta(minutes=IDLE_MINUTES),
        absolute: timedelta = timedelta(hours=ABSOLUTE_HOURS),
    ) -> None:
        self._secret_path = secret_path
        self._secret: bytes | None = None
        self._clock = clock
        self.idle = idle
        self.absolute = absolute

    @property
    def secret_path(self) -> Path:
        return self._secret_path

    def now(self) -> datetime:
        return self._clock()

    def _key(self) -> bytes:
        if self._secret is None:
            self._secret = load_or_create_secret(self._secret_path)
        return self._secret

    def issue(
        self,
        tier: str,
        token_version: int,
        *,
        session_id: str | None = None,
        absolute_expires_at: datetime | None = None,
    ) -> tuple[str, TokenClaims]:
        """Sign a token. Pass ``absolute_expires_at`` to carry the cap forward on re-issue."""
        if tier not in ALL_TIERS:
            raise ValueError(f"unknown tier {tier!r}")
        if tier == "hirer" and session_id is None:
            session_id = secrets.token_urlsafe(16)
        if tier != "hirer" and session_id is not None:
            raise ValueError("only hirer tokens carry a session id")
        now = self._clock()
        # Real elapsed time, not wall-clock time: across a daylight-saving
        # change a wall-clock 12-hour cap would be 11 or 13 real hours.
        aexp = (
            absolute_expires_at
            if absolute_expires_at is not None
            else elapsed_after(now, self.absolute)
        )
        exp = min(elapsed_after(now, self.idle), aexp)
        payload: dict[str, Any] = {
            "tier": tier,
            "tv": token_version,
            "iat": int(now.timestamp()),
            "exp": int(exp.timestamp()),
            "aexp": int(aexp.timestamp()),
        }
        if session_id is not None:
            payload["sid"] = session_id
        token = jwt.encode(payload, self._key(), algorithm=JWT_ALGORITHM)
        claims = TokenClaims(
            tier=tier,
            token_version=token_version,
            issued_at=_from_unix(payload["iat"]),
            expires_at=_from_unix(payload["exp"]),
            absolute_expires_at=_from_unix(payload["aexp"]),
            session_id=session_id,
        )
        return token, claims

    def reissue(
        self, claims: TokenClaims, *, token_version: int | None = None
    ) -> tuple[str, TokenClaims]:
        """A fresh idle window for an existing session; the absolute cap is unchanged."""
        return self.issue(
            claims.tier,
            claims.token_version if token_version is None else token_version,
            session_id=claims.session_id,
            absolute_expires_at=claims.absolute_expires_at,
        )

    def decode(self, token: str) -> TokenClaims:
        """Verify the signature and both expiries. Raises :class:`TokenError`.

        ``exp`` and ``aexp`` are checked against the injected clock rather
        than by the JWT library, so the same clock governs issue and expiry.
        """
        try:
            payload = jwt.decode(
                token,
                self._key(),
                algorithms=[JWT_ALGORITHM],
                options={
                    # Expiry is checked below against the injected clock.
                    "verify_exp": False,
                    "verify_iat": False,
                    "require": ["tier", "tv", "iat", "exp", "aexp"],
                },
            )
        except jwt.PyJWTError as exc:
            raise TokenError("invalid", "The session token is not valid") from exc
        tier = payload.get("tier")
        if tier not in ALL_TIERS:
            raise TokenError("invalid", "The session token is not valid")
        sid = payload.get("sid")
        if tier == "hirer" and not isinstance(sid, str):
            raise TokenError("invalid", "The session token is not valid")
        try:
            claims = TokenClaims(
                tier=tier,
                token_version=int(payload["tv"]),
                issued_at=_from_unix(int(payload["iat"])),
                expires_at=_from_unix(int(payload["exp"])),
                absolute_expires_at=_from_unix(int(payload["aexp"])),
                session_id=sid if tier == "hirer" else None,
            )
        except (TypeError, ValueError) as exc:
            raise TokenError("invalid", "The session token is not valid") from exc
        now = self._clock()
        if now >= claims.absolute_expires_at:
            raise TokenError("expired", "The session has reached its 12-hour limit")
        if now >= claims.expires_at:
            raise TokenError("expired", "The session has expired")
        return claims


def _from_unix(seconds: int) -> datetime:
    return datetime.fromtimestamp(seconds, tz=AUCKLAND)


class HirerAdmission(Protocol):
    """The in-memory hirer access check (:class:`proskenion.core.hirer_access.HirerAccess`)."""

    async def check(self, db: Database, claims: TokenClaims) -> None: ...


async def check_live_version(db: Database, claims: TokenClaims, hirer: HirerAdmission) -> None:
    """Refuse a token whose ``token_version`` no longer matches the account (§6.4).

    Hirer tokens are checked against ``state.hirer`` in memory, never the
    database, and are also refused once access is disabled; both surface as
    ``hirer_revoked`` so the client can say why rather than show a login.
    """
    if claims.is_hirer:
        await hirer.check(db, claims)
        return
    user = await users_crud.get_by_tier(db, claims.tier)
    if user is None or user.token_version != claims.token_version:
        raise TokenError("revoked", "The session is no longer valid")


# -- audit log (§6.14) ----------------------------------------------------------


async def record_event(
    db: Database,
    event_type: str,
    *,
    user_ident: str | None,
    ip_address: str | None,
    detail: Mapping[str, Any] | None = None,
) -> int:
    """Write one ``security_events`` row. ``detail`` is serialised as JSON."""
    return await security_events.insert(
        db,
        event_type,
        user_ident=user_ident,
        ip_address=ip_address,
        detail=None if detail is None else json.dumps(detail, sort_keys=True),
    )
