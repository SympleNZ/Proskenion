"""Schema validation (§5.5 *Field definition*, *Cross-field validation*)."""

from __future__ import annotations

import pytest

from proskenion.core.drivers.fields import (
    ConfigError,
    Field,
    apply_defaults,
    as_detail,
    is_visible,
    validate,
)

SCHEMA = [
    Field("name", type="string", label="Name", required=True, pattern=r"[a-z]+"),
    Field("channel", type="int", label="Channel", default=1, min=1, max=16),
    Field("port", type="port", label="Port", required=True),
    Field("secure", type="bool", label="Secure", default=False),
    Field(
        "mode",
        type="enum",
        label="Mode",
        default="auto",
        options=[("auto", "Automatic"), ("manual", "Manual")],
    ),
    Field(
        "manual_level",
        type="int",
        label="Manual level",
        required=True,
        min=0,
        max=100,
        depends_on=("mode", "manual"),
    ),
    Field("password", type="password", label="Password", encrypted=True),
]


def _errors(values: dict[str, object]) -> dict[str, list[str]]:
    return as_detail(validate(SCHEMA, values))


def test_valid_values_produce_no_errors() -> None:
    assert validate(SCHEMA, {"name": "abc", "port": 4352}) == []


def test_required_field_missing() -> None:
    assert _errors({"port": 1}) == {"name": ["required"]}


def test_empty_string_counts_as_missing() -> None:
    assert _errors({"name": "", "port": 1}) == {"name": ["required"]}


def test_min_max_on_int_and_port() -> None:
    detail = _errors({"name": "abc", "port": 70000, "channel": 0})
    assert detail == {"channel": ["must be at least 1"], "port": ["must be at most 65535"]}


def test_type_mismatches_are_per_field() -> None:
    detail = _errors({"name": 3, "port": "4352", "secure": "yes", "channel": True})
    assert set(detail) == {"name", "port", "secure", "channel"}
    assert detail["channel"] == ["must be a whole number"]


def test_pattern_on_strings() -> None:
    assert _errors({"name": "ABC", "port": 1}) == {"name": ["does not match the expected format"]}


def test_enum_rejects_unknown_option() -> None:
    detail = _errors({"name": "abc", "port": 1, "mode": "off"})
    assert detail == {"mode": ["must be one of: auto, manual"]}


def test_hidden_dependent_field_is_ignored() -> None:
    # mode defaults to "auto" so manual_level is hidden and its required flag does not fire.
    assert validate(SCHEMA, {"name": "abc", "port": 1}) == []
    # Even a wrong value is ignored while hidden.
    assert validate(SCHEMA, {"name": "abc", "port": 1, "manual_level": 500}) == []


def test_visible_dependent_field_is_validated() -> None:
    assert _errors({"name": "abc", "port": 1, "mode": "manual"}) == {"manual_level": ["required"]}
    detail = _errors({"name": "abc", "port": 1, "mode": "manual", "manual_level": 500})
    assert detail == {"manual_level": ["must be at most 100"]}


def test_is_visible_uses_controller_default() -> None:
    field = SCHEMA[5]
    assert not is_visible(field, SCHEMA, {})
    assert is_visible(field, SCHEMA, {"mode": "manual"})


def test_unknown_key_is_reported() -> None:
    assert _errors({"name": "abc", "port": 1, "host": "10.0.0.1"}) == {
        "host": ["not a recognised field"]
    }


def test_encrypted_form_is_accepted_for_encrypted_field() -> None:
    values = {"name": "abc", "port": 1, "password": {"enc": "AAAA"}}
    assert validate(SCHEMA, values) == []
    # But a plain-looking dict for a non-encrypted string is not text.
    assert _errors({"name": {"enc": "x"}, "port": 1}) == {"name": ["must be text"]}


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("10.2.30.71", True),
        ("fe80::1", True),
        ("projector.local", True),
        ("projector", True),
        ("10.2.30.71 ", False),
        ("", False),
        ("-bad.example", False),
        (42, False),
    ],
)
def test_host_type(value: object, ok: bool) -> None:
    schema = [Field("host", type="host", label="Host", required=True)]
    assert (validate(schema, {"host": value}) == []) is ok


def test_device_path_must_be_absolute() -> None:
    schema = [Field("device_path", type="device_path", label="Port", required=True)]
    assert validate(schema, {"device_path": "ttyUSB0"}) == [
        ConfigError(
            "device_path", "must be an absolute path, or a COM port for development on Windows"
        )
    ]
    assert validate(schema, {"device_path": "/dev/serial/by-id/usb-x-if00-port0"}) == []


def test_device_path_accepts_a_windows_com_port() -> None:
    # Serial drivers are developed against real hardware from Windows as well
    # as deployed on the Linux appliance (§7.5, §7.2.4).
    schema = [Field("device_path", type="device_path", label="Port", required=True)]
    assert validate(schema, {"device_path": "COM3"}) == []
    assert validate(schema, {"device_path": "com12"}) == []


def test_apply_defaults_fills_absent_only() -> None:
    merged = apply_defaults(SCHEMA, {"name": "abc", "channel": 5})
    assert merged["channel"] == 5
    assert merged["mode"] == "auto"
    assert merged["secure"] is False
    assert "port" not in merged  # no default, stays absent


def test_as_detail_groups_messages_per_field() -> None:
    errors = [ConfigError("a", "one"), ConfigError("b", "two"), ConfigError("a", "three")]
    assert as_detail(errors) == {"a": ["one", "three"], "b": ["two"]}


def test_field_vocabulary_is_closed() -> None:
    with pytest.raises(ValueError, match="unknown type"):
        Field("colour", type="colour", label="Colour")  # type: ignore[arg-type]


def test_field_definition_sanity() -> None:
    with pytest.raises(ValueError, match="enum needs options"):
        Field("mode", type="enum", label="Mode")
    with pytest.raises(ValueError, match="only password fields"):
        Field("token", type="string", label="Token", encrypted=True)
    with pytest.raises(ValueError, match="invalid pattern"):
        Field("name", type="string", label="Name", pattern="(")
