"""Registry: explicit registration, addressing rejection, declared-maximum capabilities."""

from __future__ import annotations

import inspect

import pytest

from proskenion.core.drivers import registry
from proskenion.core.drivers.base import ProbeResult
from proskenion.core.drivers.capabilities import MatrixCapabilities
from proskenion.core.drivers.categories import Category
from proskenion.core.drivers.fields import Field
from proskenion.core.transport.loopback import LoopbackTransport
from proskenion.core.transport.tcp import TCP_SCHEMA, TcpTransport
from tests.stubs.echo_driver import EchoDriver, RecordingSink

pytestmark = pytest.mark.usefixtures("clean_registry")


def test_register_adds_by_category_and_key() -> None:
    registry.register(EchoDriver)
    assert registry.DRIVERS[(Category.VIDEO_MATRIX, "echo")] is EchoDriver
    assert registry.get(Category.VIDEO_MATRIX, "echo") is EchoDriver


def test_unknown_driver() -> None:
    with pytest.raises(registry.UnknownDriver):
        registry.get(Category.MIXER, "nope")


def test_duplicate_key_is_rejected() -> None:
    registry.register(EchoDriver)

    class Other(EchoDriver):
        pass

    with pytest.raises(registry.RegistrationError, match="already registered"):
        registry.register(Other)


@pytest.mark.parametrize(
    "field",
    [
        Field("host", type="host", label="IP address"),
        Field("device_path", type="device_path", label="Port"),
        Field("address", type="host", label="Address"),
        Field("host", type="string", label="Host"),
    ],
)
def test_driver_schema_may_not_carry_addressing(field: Field) -> None:
    class Addressed(EchoDriver):
        key = "addressed"
        CONFIG_SCHEMA = [field]

    with pytest.raises(registry.RegistrationError, match="addressing belongs to the transport"):
        registry.register(Addressed)


def test_capabilities_attribute_is_rejected() -> None:
    class Static(EchoDriver):
        key = "static"
        capabilities = MatrixCapabilities(1, 1, True)  # type: ignore[assignment]

    with pytest.raises(registry.RegistrationError, match="must be a method"):
        registry.register(Static)


def test_unknown_transport_is_rejected() -> None:
    class Bad(EchoDriver):
        key = "bad"
        SUPPORTED_TRANSPORTS = ["carrier_pigeon"]
        TRANSPORT_DEFAULTS = {}

    with pytest.raises(registry.RegistrationError, match="unknown transport"):
        registry.register(Bad)


def test_abstract_driver_is_rejected() -> None:
    class Incomplete(registry.Driver):
        key = "incomplete"
        category = Category.MIXER
        name = "Incomplete"
        SUPPORTED_TRANSPORTS = ["loopback"]

    with pytest.raises(registry.RegistrationError, match="abstract"):
        registry.register(Incomplete)


def test_every_registered_driver_has_capabilities_as_a_method() -> None:
    registry.register(EchoDriver)
    for driver_cls in registry.DRIVERS.values():
        assert inspect.isfunction(inspect.getattr_static(driver_cls, "capabilities"))


def test_available_reports_declared_maximum_without_connecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry.register(EchoDriver)
    opened: list[str] = []

    async def spy_open(self: LoopbackTransport) -> None:
        opened.append("open")

    monkeypatch.setattr(LoopbackTransport, "open", spy_open)
    infos = registry.available(Category.VIDEO_MATRIX)
    assert opened == []
    assert len(infos) == 1
    info = infos[0]
    assert info.key == "echo" and info.name == "Echo test driver"
    assert info.capabilities == MatrixCapabilities(2, 2, True)
    assert [t.type for t in info.transports] == ["loopback", "tcp", "serial"]
    tcp = info.transports[1]
    assert tcp.schema == tuple(TCP_SCHEMA)
    assert tcp.defaults == {"port": 7}
    assert [f.key for f in info.config_schema] == ["greeting", "retries"]
    assert registry.available(Category.MIXER) == []
    assert [i.key for i in registry.available()] == ["echo"]


async def test_build_returns_a_ready_driver() -> None:
    registry.register(EchoDriver)
    sink = RecordingSink()
    driver = await registry.build(
        3,
        Category.VIDEO_MATRIX,
        "echo",
        {"transport": {"type": "tcp", "host": "10.2.30.71"}, "driver": {}},
        sink,
    )
    assert isinstance(driver, EchoDriver)
    assert isinstance(driver.transport, TcpTransport)
    assert driver.transport.config == {"host": "10.2.30.71", "port": 7}  # default applied
    assert driver.config == {"greeting": "PING", "retries": 1}
    assert driver.device_id == 3


async def test_build_reports_per_field_errors_with_section_prefix() -> None:
    registry.register(EchoDriver)
    with pytest.raises(registry.ConfigValidationError) as info:
        await registry.build(
            3,
            Category.VIDEO_MATRIX,
            "echo",
            {"transport": {"type": "tcp", "port": 0}, "driver": {"greeting": "lower"}},
            RecordingSink(),
        )
    assert info.value.detail == {
        "transport.host": ["required"],
        "transport.port": ["must be at least 1"],
        "driver.greeting": ["does not match the expected format"],
    }


async def test_build_rejects_unsupported_transport_and_bad_shape() -> None:
    registry.register(EchoDriver)
    with pytest.raises(registry.ConfigValidationError) as info:
        await registry.build(
            1, Category.VIDEO_MATRIX, "echo", {"transport": {"type": "udp"}}, RecordingSink()
        )
    assert info.value.detail == {"transport.type": ["must be one of: loopback, tcp, serial"]}
    with pytest.raises(registry.ConfigValidationError) as info:
        await registry.build(1, Category.VIDEO_MATRIX, "echo", {}, RecordingSink())
    assert "transport" in info.value.detail


async def test_build_runs_cross_field_validation() -> None:
    registry.register(EchoDriver)
    with pytest.raises(registry.ConfigValidationError) as info:
        await registry.build(
            1,
            Category.VIDEO_MATRIX,
            "echo",
            {"transport": {"type": "loopback"}, "driver": {"greeting": "BAD"}},
            RecordingSink(),
        )
    assert info.value.detail == {"driver.greeting": ["this device rejects BAD"]}


async def test_built_driver_probes_over_its_transport() -> None:
    registry.register(EchoDriver)
    driver = await registry.build(
        1, Category.VIDEO_MATRIX, "echo", {"transport": {"type": "loopback"}}, RecordingSink()
    )
    assert isinstance(driver.transport, LoopbackTransport)
    driver.transport.responder = lambda data: b"PONG\n"
    await driver.connect()
    assert await driver.probe() == ProbeResult(True)
    await driver.disconnect()


def test_no_shipped_drivers_yet_and_loading_is_explicit() -> None:
    from proskenion.core import drivers

    drivers.load_shipped_drivers()
    drivers.load_shipped_drivers()  # idempotent
    assert registry.available() == []
