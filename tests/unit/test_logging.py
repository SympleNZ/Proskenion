"""Structured JSON logging (§4.10)."""

import json
import logging
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from proskenion.config import AppSection, Config, DatabaseSection, Environment, LoggingSection
from proskenion.logging import (
    APPLICATION_LOG_NAME,
    bind_request_id,
    configure_logging,
    get_request_id,
    reload_levels,
    reset_request_id,
    shutdown_logging,
)

BASE_FIELDS = {"timestamp", "level", "logger", "message"}
# 2026-09-04T14:30:00.000+12:00 — offset is mandatory (§4.9)
TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}[+-]\d{2}:\d{2}$")


def make_config(tmp_path: Path, environment: Environment = Environment.PRODUCTION) -> Config:
    return Config(
        database=DatabaseSection(path=tmp_path / "db.sqlite"),
        logging=LoggingSection(path=tmp_path / "logs"),
        app=AppSection(environment=environment),
    )


@pytest.fixture
def log_file(tmp_path: Path) -> Iterator[Path]:
    configure_logging(make_config(tmp_path))
    try:
        yield tmp_path / "logs" / APPLICATION_LOG_NAME
    finally:
        shutdown_logging()


def read_lines(log_file: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in log_file.read_text("utf-8").splitlines()]


def test_line_is_json_with_base_fields(log_file: Path) -> None:
    logging.getLogger("proskenion.test").info("hello %s", "world")
    (line,) = read_lines(log_file)
    assert BASE_FIELDS <= set(line)
    assert line["level"] == "INFO"
    assert line["logger"] == "proskenion.test"
    assert line["message"] == "hello world"
    assert TIMESTAMP.match(line["timestamp"]), line["timestamp"]
    assert line["timestamp"][-6:] in ("+12:00", "+13:00")  # NZST / NZDT
    assert "request_id" not in line


def test_extra_becomes_context_keys(log_file: Path) -> None:
    logging.getLogger("proskenion.core.knx").warning(
        "telegram dropped", extra={"device": "knxd", "scene_id": 7, "path": Path("/x")}
    )
    (line,) = read_lines(log_file)
    assert line["device"] == "knxd"
    assert line["scene_id"] == 7
    assert line["path"] == str(Path("/x"))  # non-JSON values are stringified


def test_request_id_included_while_bound(log_file: Path) -> None:
    assert get_request_id() is None
    token = bind_request_id("01ARZ3NDEKTSV4RRFFQ69G5FAV")
    try:
        assert get_request_id() == "01ARZ3NDEKTSV4RRFFQ69G5FAV"
        logging.getLogger("proskenion").info("inside")
    finally:
        reset_request_id(token)
    logging.getLogger("proskenion").info("outside")
    inside, outside = read_lines(log_file)
    assert inside["request_id"] == "01ARZ3NDEKTSV4RRFFQ69G5FAV"
    assert "request_id" not in outside
    assert get_request_id() is None


def test_exception_traceback_is_a_field(log_file: Path) -> None:
    try:
        raise ValueError("bad value")
    except ValueError:
        logging.getLogger("proskenion").exception("failed")
    (line,) = read_lines(log_file)
    assert line["level"] == "ERROR"
    assert "Traceback" in line["exception"]
    assert "ValueError: bad value" in line["exception"]


def test_each_record_is_one_line(log_file: Path) -> None:
    logging.getLogger("proskenion").info("line one\nstill line one")
    text = log_file.read_text("utf-8")
    assert text.count("\n") == 1
    assert json.loads(text)["message"] == "line one\nstill line one"


def test_no_stderr_handler_in_production(tmp_path: Path) -> None:
    configure_logging(make_config(tmp_path))
    try:
        streams = [
            h.stream for h in logging.getLogger().handlers if type(h) is logging.StreamHandler
        ]
        assert sys.stderr not in streams
    finally:
        shutdown_logging()


def test_stderr_handler_in_development(tmp_path: Path) -> None:
    configure_logging(make_config(tmp_path, Environment.DEVELOPMENT))
    try:
        streams = [
            h.stream for h in logging.getLogger().handlers if type(h) is logging.StreamHandler
        ]
        assert sys.stderr in streams
    finally:
        shutdown_logging()


def test_reconfigure_replaces_handlers(tmp_path: Path) -> None:
    configure_logging(make_config(tmp_path / "a"))
    before = len(logging.getLogger().handlers)
    configure_logging(make_config(tmp_path / "b"))
    try:
        assert len(logging.getLogger().handlers) == before
        assert (tmp_path / "b" / "logs" / APPLICATION_LOG_NAME).exists()
    finally:
        shutdown_logging()


def test_reload_levels_sets_and_reverts() -> None:
    knx = logging.getLogger("proskenion.core.knx")
    mixer = logging.getLogger("proskenion.core.mixer")
    try:
        reload_levels({"proskenion.core.knx": "DEBUG", "proskenion.core.mixer": "warning"})
        assert knx.level == logging.DEBUG
        assert mixer.level == logging.WARNING
        reload_levels({"proskenion.core.knx": "DEBUG"})
        assert knx.level == logging.DEBUG
        assert mixer.level == logging.NOTSET
        reload_levels({})
        assert knx.level == logging.NOTSET
    finally:
        reload_levels({})


def test_reload_levels_rejects_unknown_level_without_changing_anything() -> None:
    knx = logging.getLogger("proskenion.core.knx")
    with pytest.raises(ValueError, match="VERBOSE"):
        reload_levels({"proskenion.core.knx": "VERBOSE"})
    assert knx.level == logging.NOTSET


# -- redaction (§4.10, §6.14): a secret-looking ``extra=`` key is never logged -------


def test_extra_with_a_sensitive_key_is_redacted(log_file: Path) -> None:
    logging.getLogger("proskenion.core.auth").warning(
        "sign-in attempt",
        extra={"password": "hunter2", "new_password": "hunter3", "pin": "246810", "ip": "10.0.0.9"},
    )
    (line,) = read_lines(log_file)
    assert line["password"] == "[redacted]"
    assert line["new_password"] == "[redacted]"
    assert line["pin"] == "[redacted]"
    assert line["ip"] == "10.0.0.9"  # not a credential — left alone


def test_extra_redaction_applies_at_debug_too(log_file: Path) -> None:
    logger = logging.getLogger("proskenion.core.mixer")
    logger.setLevel(logging.DEBUG)
    logger.debug("state sync", extra={"api_token": "abc123", "channel": 4})
    (line,) = read_lines(log_file)
    assert line["level"] == "DEBUG"
    assert line["api_token"] == "[redacted]"
    assert line["channel"] == 4


def test_redaction_does_not_catch_token_version(log_file: Path) -> None:
    """``token_version`` (§6.14's audit detail) is a plain counter, not a
    credential — the bare fragment "token" is deliberately not matched."""
    logging.getLogger("proskenion.core.auth").info("password changed", extra={"token_version": 3})
    (line,) = read_lines(log_file)
    assert line["token_version"] == 3


def test_redaction_recurses_into_nested_dicts_and_lists(log_file: Path) -> None:
    headers = {"Authorization": "Bearer xyz", "Accept": "*/*"}
    logging.getLogger("proskenion.api.auth").warning(
        "denied", extra={"headers": headers, "attempts": [{"pin": "1"}]}
    )
    (line,) = read_lines(log_file)
    assert line["headers"] == {"Authorization": "[redacted]", "Accept": "*/*"}
    assert line["attempts"] == [{"pin": "[redacted]"}]
