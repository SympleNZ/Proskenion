"""``python -m proskenion.tools.verify`` (§13.4): the monthly check's entry point."""

import asyncio
import io
from pathlib import Path

import pytest

from proskenion.db.connection import Database
from proskenion.db.migrations import migrate
from proskenion.tools import verify as tool

CONFIG = """\
[database]
path = "{db}"

[logging]
path = "{logs}"

[app]
state_dir = "{state}"
data_dir = "{data}"
"""


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        CONFIG.format(
            db=(tmp_path / "auditorium.db").as_posix(),
            logs=(tmp_path / "logs").as_posix(),
            state=(tmp_path / "appliance").as_posix(),
            data=(tmp_path / "data").as_posix(),
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


def test_archive_names_the_one_to_check() -> None:
    assert tool.parse_args(["--archive", "auditorium-20260927-0301"]).archive == (
        "auditorium-20260927-0301"
    )
    assert tool.parse_args([]).archive is None


def test_an_unknown_archive_is_a_usage_error_not_a_failed_check(config_path: Path) -> None:
    out, err = io.StringIO(), io.StringIO()
    code = tool.main(
        ["--config", str(config_path), "--archive", "auditorium-20990101-0000"],
        stdout=out,
        stderr=err,
    )
    assert code == tool.EXIT_CONFIG_ERROR
    assert "auditorium-20990101-0000" in err.getvalue()
