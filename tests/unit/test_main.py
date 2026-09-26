"""Entry point: argument parsing, configuration failure, uvicorn invocation."""

import asyncio
import logging
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI

from proskenion import main as entry
from proskenion.api.app import ACCESS_LOGGER
from proskenion.config import parse_config
from proskenion.core.dmx.endpoint import ArtNetEndpoint
from proskenion.db.connection import Database
from proskenion.db.migrations import SCHEMA_AHEAD_MESSAGE, MigrationFailed, migrate
from proskenion.logging import APPLICATION_LOG_NAME, shutdown_logging

CONFIG = """\
[database]
path = "{db}"

[server]
host = "127.0.0.1"
port = 8765

[logging]
path = "{logs}"
"""


def test_exit_codes() -> None:
    assert entry.EXIT_OK == 0
    assert entry.EXIT_CONFIG_ERROR == 1
    assert entry.EXIT_MIGRATION_FAILED == 2
    assert entry.EXIT_DATA_UNAVAILABLE == 4
    assert entry.EXIT_DISK_FULL == 5
    codes = (
        entry.EXIT_OK,
        entry.EXIT_CONFIG_ERROR,
        entry.EXIT_MIGRATION_FAILED,
        entry.EXIT_DATA_UNAVAILABLE,
        entry.EXIT_DISK_FULL,
    )
    assert len(set(codes)) == len(codes)  # §14.5: the rollback tells them apart


def test_parse_args_config() -> None:
    assert entry.parse_args(["--config", "/tmp/x.toml"]).config == "/tmp/x.toml"
    assert entry.parse_args([]).config is None


def test_bad_config_exits_with_config_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    status = entry.main(["--config", str(tmp_path / "missing.toml")])
    assert status == entry.EXIT_CONFIG_ERROR
    assert "not found" in capsys.readouterr().err


def test_main_runs_uvicorn_with_proxy_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        CONFIG.format(db=(tmp_path / "a.db").as_posix(), logs=(tmp_path / "logs").as_posix()),
        encoding="utf-8",
    )
    calls: list[tuple[Any, dict[str, Any]]] = []

    def fake_run(app: Any, **kwargs: Any) -> None:
        calls.append((app, kwargs))

    monkeypatch.setattr(uvicorn, "run", fake_run)
    try:
        assert entry.main(["--config", str(config_path)]) == entry.EXIT_OK
    finally:
        shutdown_logging()

    (app, kwargs) = calls[0]
    assert isinstance(app, FastAPI)
    assert kwargs["host"] == "127.0.0.1"
    assert kwargs["port"] == 8765
    assert kwargs["proxy_headers"] is True
    assert kwargs["forwarded_allow_ips"] == "127.0.0.1"
    assert kwargs["log_config"] is None
    assert (tmp_path / "logs" / APPLICATION_LOG_NAME).exists()
    assert logging.getLogger().level == logging.INFO


# -- §15.2: a migration failure is exit code 2 ----------------------------------


def write_config(tmp_path: Path, db_name: str = "a.db") -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        CONFIG.format(db=(tmp_path / db_name).as_posix(), logs=(tmp_path / "logs").as_posix()),
        encoding="utf-8",
    )
    return path


def test_a_failed_migration_exits_two_not_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = write_config(tmp_path)

    async def boom(db: Any, *args: Any, **kwargs: Any) -> list[str]:
        raise MigrationFailed("003_add_bars.sql", RuntimeError("no such column: colour"))

    monkeypatch.setattr("proskenion.core.lifecycle.migrate", boom)
    monkeypatch.setattr(uvicorn, "run", _must_not_run)
    try:
        status = entry.main(["--config", str(config_path)])
    finally:
        shutdown_logging()
        entry.shutdown_access_logging()

    assert status == entry.EXIT_MIGRATION_FAILED == 2
    assert status != entry.EXIT_CONFIG_ERROR  # §14.5 tells the two apart
    assert "003_add_bars.sql" in capsys.readouterr().err


async def _prepare_ahead_database(path: Path) -> None:
    """A database recording a migration newer than anything this build ships."""
    db = Database()
    await db.open(path)
    try:
        await migrate(db)
        async with db.write() as conn:
            await conn.execute(
                "INSERT INTO schema_versions (migration, applied_at) VALUES (?, ?)",
                ("999_from_the_future.sql", "2027-01-01T00:00:00+13:00"),
            )
    finally:
        await db.close()


def test_a_database_ahead_of_the_code_exits_two_with_the_spec_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    config_path = write_config(tmp_path, "ahead.db")
    asyncio.run(_prepare_ahead_database(tmp_path / "ahead.db"))
    monkeypatch.setattr(uvicorn, "run", _must_not_run)
    try:
        with caplog.at_level(logging.ERROR, logger="proskenion.main"):
            status = entry.main(["--config", str(config_path)])
    finally:
        shutdown_logging()
        entry.shutdown_access_logging()

    assert status == entry.EXIT_MIGRATION_FAILED
    message = capsys.readouterr().err
    assert SCHEMA_AHEAD_MESSAGE in message
    assert any(SCHEMA_AHEAD_MESSAGE in record.getMessage() for record in caplog.records)


def test_an_unwritable_data_directory_exits_with_its_own_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = write_config(tmp_path)
    original = Path.write_text

    def deny(self: Path, *args: Any, **kwargs: Any) -> int:
        if self.name.startswith(".proskenion-write-probe"):
            raise PermissionError("Read-only file system")
        return original(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "write_text", deny)
    monkeypatch.setattr(uvicorn, "run", _must_not_run)
    try:
        status = entry.main(["--config", str(config_path)])
    finally:
        shutdown_logging()
        entry.shutdown_access_logging()

    assert status == entry.EXIT_DATA_UNAVAILABLE
    assert status not in (entry.EXIT_CONFIG_ERROR, entry.EXIT_MIGRATION_FAILED)
    assert "Read-only file system" in capsys.readouterr().err


def test_a_full_data_directory_exits_with_its_own_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """§14.5, Q12: below 100 MB free, ``disk_full`` — never mistaken for the
    other emergency-mode exit status, or for a rollback-worthy update failure."""
    config_path = write_config(tmp_path)

    class FakeUsage:
        free = 50 * 1024 * 1024  # under the 100 MB floor

    monkeypatch.setattr("proskenion.core.lifecycle.shutil.disk_usage", lambda path: FakeUsage())
    monkeypatch.setattr(uvicorn, "run", _must_not_run)
    try:
        status = entry.main(["--config", str(config_path)])
    finally:
        shutdown_logging()
        entry.shutdown_access_logging()

    message = capsys.readouterr().err
    assert status == entry.EXIT_DISK_FULL == 5
    assert status not in (
        entry.EXIT_CONFIG_ERROR,
        entry.EXIT_MIGRATION_FAILED,
        entry.EXIT_DATA_UNAVAILABLE,
    )
    assert "byte" in message


def _must_not_run(app: Any, **kwargs: Any) -> None:
    raise AssertionError("the server must not start after a refusal to start")


# -- §4.10: the access log ------------------------------------------------------


def test_uvicorn_access_log_is_replaced_by_ours(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = write_config(tmp_path)
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: calls.append(kwargs))
    try:
        assert entry.main(["--config", str(config_path)]) == entry.EXIT_OK
    finally:
        shutdown_logging()
        entry.shutdown_access_logging()

    assert calls[0]["access_log"] is False  # AccessLogMiddleware writes them
    assert (tmp_path / "logs" / entry.ACCESS_LOG_NAME).exists()
    assert logging.getLogger(ACCESS_LOGGER).propagate is False


# -- §7.2.5: the e2e Art-Net port hook, development-only ------------------------

MINIMAL = {"database": {"path": "x.db"}, "logging": {"path": "logs"}}


def test_apply_test_hooks_overrides_the_artnet_port_in_development(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(entry.ARTNET_PORT_ENV, "54321")
    config = parse_config({**MINIMAL, "app": {"environment": "development"}})
    entry.apply_test_hooks(config)
    assert ArtNetEndpoint.default_port == 54321


def test_apply_test_hooks_leaves_production_alone_even_with_the_variable_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Production is the default when [app] is absent, exactly as a real
    # appliance's config.toml has it: the variable being set in the
    # environment must never be enough on its own (§4.14).
    monkeypatch.setenv(entry.ARTNET_PORT_ENV, "54321")
    config = parse_config(MINIMAL)
    before = ArtNetEndpoint.default_port
    entry.apply_test_hooks(config)
    assert ArtNetEndpoint.default_port == before
    assert ArtNetEndpoint.default_port != 54321


def test_apply_test_hooks_does_nothing_without_the_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(entry.ARTNET_PORT_ENV, raising=False)
    config = parse_config({**MINIMAL, "app": {"environment": "development"}})
    before = ArtNetEndpoint.default_port
    entry.apply_test_hooks(config)
    assert ArtNetEndpoint.default_port == before


def test_main_applies_the_artnet_hook_before_serving(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole entry point, not just the helper: a real e2e appliance's path."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        CONFIG.format(db=(tmp_path / "a.db").as_posix(), logs=(tmp_path / "logs").as_posix())
        + '\n[app]\nenvironment = "development"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv(entry.ARTNET_PORT_ENV, "54322")
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: None)
    try:
        assert entry.main(["--config", str(config_path)]) == entry.EXIT_OK
    finally:
        shutdown_logging()
        entry.shutdown_access_logging()
    assert ArtNetEndpoint.default_port == 54322
