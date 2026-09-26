"""The console reset tool (§6.9)."""

import asyncio
import io
import json
from pathlib import Path

import pytest

from proskenion.config import load_config
from proskenion.core import auth
from proskenion.core.hirer_access import ACCESS_SIGNAL_FILENAME
from proskenion.core.ratelimit import RateLimiter, Scope
from proskenion.db.connection import Database
from proskenion.db.crud import hirer as hirer_crud
from proskenion.db.crud import security_events
from proskenion.db.crud import users as users_crud
from proskenion.db.migrations import migrate
from proskenion.tools import reset_password as tool

CONFIG = """\
[database]
path = "{db}"

[logging]
path = "{logs}"

[app]
state_dir = "{state}"
"""


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        CONFIG.format(
            db=(tmp_path / "auditorium.db").as_posix(),
            logs=(tmp_path / "logs").as_posix(),
            state=(tmp_path / "appliance").as_posix(),
        ),
        encoding="utf-8",
    )

    async def prepare() -> None:
        db = Database()
        await db.open(tmp_path / "auditorium.db")
        try:
            await migrate(db)
        finally:
            await db.close()

    asyncio.run(prepare())
    return path


async def _state(path: Path) -> tuple[users_crud.User, users_crud.User, hirer_crud.HirerConfig]:
    db = Database()
    await db.open(path)
    try:
        admin = await users_crud.get_by_tier(db, "admin")
        operator = await users_crud.get_by_tier(db, "operator")
        hirer = await hirer_crud.get(db)
        assert admin is not None and operator is not None
        return admin, operator, hirer
    finally:
        await db.close()


async def _events(path: Path, event_type: str) -> list[security_events.SecurityEvent]:
    db = Database()
    await db.open(path)
    try:
        return await security_events.query(db, event_type=event_type)
    finally:
        await db.close()


def run(config_path: Path, *args: str, stdin: str = "") -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    status = tool.main(
        ["--config", str(config_path), *args], stdin=io.StringIO(stdin), stdout=out, stderr=err
    )
    return status, out.getvalue(), err.getvalue()


def test_requires_exactly_one_action() -> None:
    with pytest.raises(SystemExit):
        tool.parse_args([])
    with pytest.raises(SystemExit):
        tool.parse_args(["--admin", "--operator"])


def test_bad_config_exits_with_config_error(tmp_path: Path) -> None:
    status, _, err = run(tmp_path / "missing.toml", "--admin", "--from-stdin", stdin="x\n")
    assert status == tool.EXIT_CONFIG_ERROR
    assert "not found" in err


def test_reset_admin_writes_hash_and_bumps_token_version(config_path: Path, tmp_path: Path) -> None:
    db_path = tmp_path / "auditorium.db"
    admin_before, operator_before, hirer_before = asyncio.run(_state(db_path))
    assert admin_before.has_placeholder_password
    assert admin_before.password_changed_at is None  # never recorded (§21.23)

    status, out, err = run(config_path, "--admin", "--from-stdin", stdin="a new admin password\n")
    assert (status, err) == (tool.EXIT_OK, "")
    assert "token_version is now 1" in out

    admin, operator, hirer = asyncio.run(_state(db_path))
    assert not admin.has_placeholder_password
    assert auth.verify_secret("a new admin password", admin.password)
    assert admin.token_version == admin_before.token_version + 1
    assert admin.password_changed_at is not None
    assert operator == operator_before and hirer == hirer_before  # nothing else touched

    changed = asyncio.run(_events(db_path, "password_changed"))
    assert len(changed) == 1
    assert changed[0].user_ident == "admin" and changed[0].ip_address is None
    assert json.loads(changed[0].detail or "{}")["via"] == "reset-tool"


def test_reset_operator(config_path: Path, tmp_path: Path) -> None:
    status, _, _ = run(config_path, "--operator", "--from-stdin", stdin="operator-new-pass\n")
    assert status == tool.EXIT_OK
    admin, operator, _ = asyncio.run(_state(tmp_path / "auditorium.db"))
    assert auth.verify_secret("operator-new-pass", operator.password)
    assert admin.has_placeholder_password


def test_short_password_changes_nothing(config_path: Path, tmp_path: Path) -> None:
    status, _, err = run(config_path, "--admin", "--from-stdin", stdin="short\n")
    assert status == tool.EXIT_BAD_SECRET
    assert "at least 12 characters" in err
    admin, _, _ = asyncio.run(_state(tmp_path / "auditorium.db"))
    assert admin.has_placeholder_password and admin.token_version == 0


def test_empty_stdin_aborts(config_path: Path) -> None:
    status, _, err = run(config_path, "--admin", "--from-stdin", stdin="")
    assert status == tool.EXIT_ABORTED
    assert "nothing changed" in err


def test_reset_hirer_pin(config_path: Path, tmp_path: Path) -> None:
    status, out, _ = run(config_path, "--hirer-pin", "--from-stdin", stdin="135790\n")
    assert status == tool.EXIT_OK
    assert "hirer sessions are invalidated" in out
    _, _, hirer = asyncio.run(_state(tmp_path / "auditorium.db"))
    assert auth.verify_secret("135790", hirer.pin)
    assert hirer.token_version == 1
    assert hirer.enabled is False  # untouched
    assert len(asyncio.run(_events(tmp_path / "auditorium.db", "pin_changed"))) == 1
    # The running service holds hirer access in memory; the signal tells it to
    # reload the row and close the sessions the new version ended.
    config = load_config(config_path)
    assert (config.app.state_dir / ACCESS_SIGNAL_FILENAME).exists()


@pytest.mark.parametrize("pin", ["12345", "1234567", "12345a", "١٢٣٤٥٦"])
def test_bad_pin_changes_nothing(config_path: Path, tmp_path: Path, pin: str) -> None:
    status, _, err = run(config_path, "--hirer-pin", "--from-stdin", stdin=f"{pin}\n")
    assert status == tool.EXIT_BAD_SECRET
    assert "PIN" in err
    _, _, hirer = asyncio.run(_state(tmp_path / "auditorium.db"))
    assert hirer.has_placeholder_pin


def test_prompt_mismatch_aborts(config_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    answers = iter(["first-password-1", "second-password-2"])
    monkeypatch.setattr(tool.getpass, "getpass", lambda prompt: next(answers))
    status, _, err = run(config_path, "--admin")
    assert status == tool.EXIT_ABORTED
    assert "did not match" in err


def test_prompt_confirmed_resets(
    config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prompts: list[str] = []

    def fake_getpass(prompt: str) -> str:
        prompts.append(prompt)
        return "a-confirmed-password"

    monkeypatch.setattr(tool.getpass, "getpass", fake_getpass)
    status, _, _ = run(config_path, "--operator")
    assert status == tool.EXIT_OK
    assert prompts == ["New operator password: ", "Confirm operator password: "]
    _, operator, _ = asyncio.run(_state(tmp_path / "auditorium.db"))
    assert auth.verify_secret("a-confirmed-password", operator.password)


def test_clear_lockouts_writes_signal_the_limiter_consumes(
    config_path: Path, tmp_path: Path
) -> None:
    signal = tmp_path / "appliance" / "clear-lockouts"
    status, out, _ = run(config_path, "--clear-lockouts")
    assert status == tool.EXIT_OK
    assert signal.exists()
    assert str(signal) in out
    assert "running service" in out

    limiter = RateLimiter(signal_path=signal)
    for _ in range(5):
        limiter.record_failure(Scope.STAFF_LOGIN, "10.0.0.1")
    limiter.check(Scope.STAFF_LOGIN, "10.0.0.1")  # cleared, not LockedOut
    assert not signal.exists()

    # Credentials were not touched.
    admin, _, hirer = asyncio.run(_state(tmp_path / "auditorium.db"))
    assert admin.has_placeholder_password and hirer.has_placeholder_pin
