"""The drivers and devices endpoints (spec §16.7, §21.24).

Every endpoint has a success test and one covering its primary failure mode,
asserting the §16.1 envelope code rather than only the status (§22.4).

The drivers registered here are test doubles: a matrix that can be told to
stop answering, a mixer with a password and optional metering, and a control
surface with a manifest. Between them they exercise every branch the Devices
screen renders.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Mapping
from dataclasses import dataclass
from typing import Any, ClassVar

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, Response

from proskenion.api.app import API_PREFIX, create_app
from proskenion.api.devices import SECRET_SENTINEL, VERSION_HEADER
from proskenion.config import Config
from proskenion.core.auth import TokenService
from proskenion.core.bus import EventBus
from proskenion.core.devices import OWNER, DeviceManager
from proskenion.core.drivers import load_shipped_drivers, registry
from proskenion.core.drivers.base import Driver, ProbeResult
from proskenion.core.drivers.capabilities import (
    ChannelRef,
    LawPoint,
    MatrixCapabilities,
    MatrixRefs,
    MixerCapabilities,
    ProjectorCapabilities,
    ProjectorState,
    SurfaceCapabilities,
    SurfaceControl,
)
from proskenion.core.drivers.categories import Category
from proskenion.core.drivers.fields import Field
from proskenion.core.ratelimit import RateLimiter
from proskenion.core.state import StateStore
from proskenion.core.transport.base import PortOption
from proskenion.db.connection import Database
from proskenion.db.crud import devices as devices_crud
from proskenion.db.crud import mixer as mixer_crud
from tests.stubs.cq_midi_stub import CqMidiStub
from tests.unit.api.conftest import ADMIN_PASSWORD, OPERATOR_PASSWORD, make_client

DEVICES = f"{API_PREFIX}/devices"
DRIVERS = f"{API_PREFIX}/drivers"
AUTH = f"{API_PREFIX}/auth"

LOOPBACK: dict[str, Any] = {"transport": {"type": "loopback"}, "driver": {}}


# -- test drivers ---------------------------------------------------------------


class ToggleDriver(Driver):
    """A matrix that answers, or does not, according to its configuration."""

    key = "toggle"
    category = Category.VIDEO_MATRIX
    name = "Toggle test matrix"
    SUPPORTED_TRANSPORTS: ClassVar[list[str]] = ["loopback", "serial"]
    TRANSPORT_DEFAULTS: ClassVar[dict[str, dict[str, Any]]] = {"serial": {"baud": 9600}}
    CONFIG_SCHEMA: ClassVar[list[Field]] = [
        Field("answer", type="bool", label="Answer probes", default=True),
    ]
    PROBE_INTERVAL = 0.05
    INITIAL_RETRY_DELAY = 0.01
    MAX_RETRY_DELAY = 0.02

    async def probe(self) -> ProbeResult:
        if self.config.get("answer", True):
            return ProbeResult(True)
        return ProbeResult(False, "no reply to PAXXR")

    def capabilities(self) -> MatrixCapabilities:
        return MatrixCapabilities(4, 2, supports_atomic_route=True)

    def available_refs(self) -> MatrixRefs:
        return MatrixRefs(
            inputs=[ChannelRef("1", "Input 1", "input", False)],
            outputs=[ChannelRef("1", "Output 1", "output", False)],
        )


class MixerStubDriver(Driver):
    """A mixer with a password, an enum, a dependent field and optional metering."""

    key = "testmixer"
    category = Category.MIXER
    name = "Test mixer"
    SUPPORTED_TRANSPORTS: ClassVar[list[str]] = ["loopback"]
    CONFIG_SCHEMA: ClassVar[list[Field]] = [
        Field(
            "mode",
            type="enum",
            label="Mode",
            default="midi",
            options=[("midi", "MIDI"), ("native", "Native")],
        ),
        Field(
            "meter_rate",
            type="int",
            label="Meter rate",
            default=10,
            min=1,
            max=50,
            depends_on=("mode", "native"),
            help="Frames per second",
        ),
        Field("password", type="password", label="Password", encrypted=True),
        Field("metering", type="bool", label="Metering", default=True),
    ]
    PROBE_INTERVAL = 0.05

    async def probe(self) -> ProbeResult:
        return ProbeResult(True)

    def capabilities(self) -> MixerCapabilities:
        metering = bool(self.config.get("metering", True))
        return MixerCapabilities(
            input_count=8,
            output_count=2,
            supports_scene_recall=False,
            supports_pan=False,
            supports_mute=True,
            supports_metering=metering,
            meter_min_db=-60.0 if metering else None,
            meter_max_db=10.0 if metering else None,
            meter_point="post_fader" if metering else None,
            supports_gain=False,
            supports_dca=False,
            min_db=-90.0,
            max_db=10.0,
        )

    def available_refs(self) -> list[ChannelRef]:
        return [ChannelRef("ip1", "Input 1", "input", False)]

    def fader_law(self) -> list[LawPoint]:
        return [LawPoint(0.0, None, "-inf"), LawPoint(1.0, 10.0, "+10", detent=True)]


class SurfaceStubDriver(Driver):
    """A control surface, so the manifest endpoint has something to return."""

    key = "testsurface"
    category = Category.CONTROL_SURFACE
    name = "Test control surface"
    SUPPORTED_TRANSPORTS: ClassVar[list[str]] = ["loopback"]
    PROBE_INTERVAL = 0.05

    async def probe(self) -> ProbeResult:
        return ProbeResult(True)

    def capabilities(self) -> SurfaceCapabilities:
        return SurfaceCapabilities(
            strip_count=8,
            has_motorised_faders=True,
            has_meters=False,
            has_scribble_strips=True,
        )

    def manifest(self) -> list[SurfaceControl]:
        return [SurfaceControl(id="fader_1", type="fader", col=0, row=0, motorised=True)]


class TcpToggleDriver(Driver):
    """A projector-category driver over real ``tcp`` addressing.

    Every other test double here uses ``loopback``, which has no address to
    mirror into the firewall. This one exists so
    ``test_the_devices_table_reaches_the_firewall`` can prove the mirror
    (``proskenion/core/system_config.py``) end to end through the real
    ``POST``/``PUT``/``DELETE /devices`` endpoints, without needing a real
    device to answer: ``probe`` never runs in these tests (create does not
    wait for one, and the address-move test disables the device so it is
    never asked to connect at all — see that test's own comment).
    """

    key = "tcptoggle"
    category = Category.PROJECTOR
    name = "TCP test projector"
    SUPPORTED_TRANSPORTS: ClassVar[list[str]] = ["tcp"]
    PROBE_INTERVAL = 0.05

    async def probe(self) -> ProbeResult:
        return ProbeResult(True)

    def capabilities(self) -> ProjectorCapabilities:
        return ProjectorCapabilities(inputs=(), supports_authentication=False)

    def current_state(self) -> ProjectorState:
        return ProjectorState.ON

    def add_state_listener(self, callback: Any) -> None:
        return None

    async def set_power(self, on: bool) -> None:
        return None

    async def set_input(self, input_ref: str) -> None:
        return None

    async def read_state(self) -> ProjectorState:
        return self.current_state()

    async def read_input(self) -> str:
        return ""


TEST_DRIVERS: tuple[type[Driver], ...] = (
    ToggleDriver,
    MixerStubDriver,
    SurfaceStubDriver,
    TcpToggleDriver,
)


# -- fixtures -------------------------------------------------------------------


@pytest.fixture(autouse=True)
def drivers(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    load_shipped_drivers()
    table = dict(registry.DRIVERS)
    for driver_cls in TEST_DRIVERS:
        table[(driver_cls.category, driver_cls.key)] = driver_cls
    monkeypatch.setattr(registry, "DRIVERS", table)
    yield


@dataclass
class Services:
    bus: EventBus
    state: StateStore
    manager: DeviceManager


@pytest.fixture
async def services(db: Database, config: Config) -> AsyncIterator[Services]:
    bus = EventBus()
    await bus.start()
    state = StateStore(config, bus)
    manager = DeviceManager(
        db, state, bus, config, connect_timeout=1.0, probe_timeout=0.5, stop_timeout=2.0
    )
    await manager.start()
    try:
        yield Services(bus, state, manager)
    finally:
        await manager.stop()
        await bus.stop()


@pytest.fixture
def app(
    config: Config,
    db: Database,
    tokens: TokenService,
    limiter: RateLimiter,
    services: Services,
) -> FastAPI:
    return create_app(
        config,
        db=db,
        tokens=tokens,
        limiter=limiter,
        bus=services.bus,
        state=services.state,
        devices_manager=services.manager,
    )


async def login(client: AsyncClient, password: str = ADMIN_PASSWORD) -> Response:
    return await client.post(f"{AUTH}/login", json={"password": password})


def code(response: Response) -> str:
    body: dict[str, Any] = response.json()
    return str(body["error"]["code"])


def detail(response: Response) -> dict[str, Any]:
    body: dict[str, Any] = response.json()
    return dict(body["error"]["detail"])


async def create(
    client: AsyncClient,
    *,
    category: str = "video_matrix",
    driver_key: str = "stub",
    name: str = "Auditorium matrix",
    config: Mapping[str, Any] | None = None,
    enabled: bool = True,
) -> dict[str, Any]:
    response = await client.post(
        DEVICES,
        json={
            "category": category,
            "driver_key": driver_key,
            "name": name,
            "config": dict(LOOPBACK if config is None else config),
            "enabled": enabled,
        },
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


def driver_by_key(body: dict[str, Any], key: str, category: str | None = None) -> dict[str, Any]:
    """The one driver named ``key`` — ``category`` disambiguates a key reused
    across categories, as ``"stub"`` legitimately is (mixer/stub, §5.5;
    video_matrix/stub, §7.5): the registry scopes a driver key to its
    category, not globally, so a key alone need not be unique here either."""
    matches = [
        driver
        for driver in body["drivers"]
        if driver["key"] == key and (category is None or driver["category"] == category)
    ]
    if len(matches) == 1:
        found: dict[str, Any] = matches[0]
        return found
    raise AssertionError(
        f"driver {key!r} (category={category!r}) matched {len(matches)} of "
        f"{[(d['key'], d['category']) for d in body['drivers']]}"
    )


# -- GET /drivers ---------------------------------------------------------------


async def test_drivers_carry_everything_a_generated_form_needs(client: AsyncClient) -> None:
    await login(client)

    response = await client.get(DRIVERS)

    assert response.status_code == 200
    body = response.json()
    stub = driver_by_key(body, "stub", category="video_matrix")
    assert stub["category"] == "video_matrix"
    assert stub["capabilities"] == {
        "input_count": 4,
        "output_count": 2,
        "supports_atomic_route": True,
    }
    assert [t["type"] for t in stub["transports"]] == ["loopback"]

    mixer = driver_by_key(body, "testmixer")
    by_key = {f["key"]: f for f in mixer["config_schema"]}
    # options and depends_on must survive: the form is generated from them
    assert by_key["mode"]["options"] == [
        {"value": "midi", "label": "MIDI"},
        {"value": "native", "label": "Native"},
    ]
    assert by_key["meter_rate"]["depends_on"] == {"field": "mode", "equals": "native"}
    assert by_key["meter_rate"]["help"] == "Frames per second"
    assert by_key["meter_rate"]["min"] == 1 and by_key["meter_rate"]["max"] == 50
    assert by_key["password"]["encrypted"] is True

    toggle = driver_by_key(body, "toggle")
    serial = next(t for t in toggle["transports"] if t["type"] == "serial")
    assert serial["defaults"] == {"baud": 9600}
    parity = next(f for f in serial["config_schema"] if f["key"] == "parity")
    assert {o["value"] for o in parity["options"]} == {"none", "even", "odd"}


async def test_drivers_are_admin_only(client: AsyncClient) -> None:
    await login(client, OPERATOR_PASSWORD)

    response = await client.get(DRIVERS)

    assert response.status_code == 403
    assert code(response) == "permission_denied"


# -- GET /drivers/serial-ports --------------------------------------------------


async def test_serial_ports_flag_a_port_a_configured_device_holds(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await login(client)
    await create(
        client,
        driver_key="toggle",
        name="Rack matrix",
        config={
            "transport": {
                "type": "serial",
                "device_path": "/dev/serial/by-id/usb-FTDI",
                "baud": 9600,
            },
            "driver": {},
        },
    )
    seen: dict[str, dict[str, str]] = {}

    async def fake_enumerate(
        host: Any = None, held_paths: Mapping[str, str] | None = None
    ) -> list[PortOption]:
        seen["held"] = dict(held_paths or {})
        return [
            PortOption(
                path="/dev/serial/by-id/usb-FTDI",
                label="FTDI USB-RS232 Cable",
                vendor_id="0403",
                product_id="6001",
                in_use=True,
                in_use_by="Rack matrix",
            )
        ]

    monkeypatch.setattr("proskenion.api.devices.enumerate_serial_ports", fake_enumerate)

    response = await client.get(f"{DRIVERS}/serial-ports")

    assert response.status_code == 200
    assert seen["held"] == {"/dev/serial/by-id/usb-FTDI": "Rack matrix"}
    ports = response.json()["ports"]
    assert ports[0]["in_use"] is True
    assert ports[0]["in_use_by"] == "Rack matrix"


async def test_serial_ports_need_a_session(client: AsyncClient) -> None:
    response = await client.get(f"{DRIVERS}/serial-ports")

    assert response.status_code == 401
    assert code(response) == "unauthenticated"


# -- devices CRUD ---------------------------------------------------------------


async def test_create_and_read_a_device(client: AsyncClient, services: Services) -> None:
    await login(client)

    created = await create(client)

    assert created["state_key"] == "hdmi"
    record = await services.manager.wait_for_connection(created["id"])
    assert record is not None and record.status == "connected"

    listed = await client.get(DEVICES)
    assert [d["id"] for d in listed.json()["devices"]] == [created["id"]]

    one = await client.get(f"{DEVICES}/{created['id']}")
    assert one.status_code == 200
    assert one.json()["status"]["status"] == "connected"
    assert one.json()["status"]["protocol"] == "Stub video matrix (no hardware)"


async def test_create_rejects_a_configuration_the_driver_cannot_accept(
    client: AsyncClient,
) -> None:
    await login(client)

    response = await client.post(
        DEVICES,
        json={
            "category": "video_matrix",
            "driver_key": "stub",
            "name": "Too big",
            "config": {"transport": {"type": "loopback"}, "driver": {"input_count": 99}},
        },
    )

    assert response.status_code == 422
    assert code(response) == "validation_failed"
    assert detail(response)["driver.input_count"] == ["must be at most 16"]


async def test_create_rejects_an_unknown_driver(client: AsyncClient) -> None:
    await login(client)

    response = await client.post(
        DEVICES,
        json={
            "category": "video_matrix",
            "driver_key": "not_shipped",
            "name": "Nope",
            "config": LOOPBACK,
        },
    )

    assert response.status_code == 422
    assert code(response) == "validation_failed"
    assert "driver_key" in detail(response)


async def test_listing_devices_needs_a_session(client: AsyncClient) -> None:
    response = await client.get(DEVICES)

    assert response.status_code == 401
    assert code(response) == "unauthenticated"


async def test_reading_an_unknown_device_is_not_found(client: AsyncClient) -> None:
    await login(client)

    response = await client.get(f"{DEVICES}/404")

    assert response.status_code == 404
    assert code(response) == "not_found"


# -- PUT ------------------------------------------------------------------------


async def test_put_saves_and_keeps_the_device_running(
    client: AsyncClient, services: Services
) -> None:
    await login(client)
    device = await create(client, driver_key="toggle", config=LOOPBACK)
    await services.manager.wait_for_connection(device["id"])

    response = await client.put(
        f"{DEVICES}/{device['id']}",
        json={"name": "Renamed matrix"},
        headers={VERSION_HEADER: device["updated_at"]},
    )

    assert response.status_code == 200
    assert response.json()["name"] == "Renamed matrix"
    assert services.manager.status(device["id"]) is not None


async def test_put_without_the_version_header_is_rejected(client: AsyncClient) -> None:
    await login(client)
    device = await create(client)

    response = await client.put(f"{DEVICES}/{device['id']}", json={"name": "No header"})

    assert response.status_code == 422
    assert code(response) == "validation_failed"
    assert VERSION_HEADER in detail(response)


async def test_a_stale_version_is_a_conflict_carrying_the_current_record(
    client: AsyncClient,
) -> None:
    await login(client)
    device = await create(client)
    await client.put(
        f"{DEVICES}/{device['id']}",
        json={"name": "First edit"},
        headers={VERSION_HEADER: device["updated_at"]},
    )

    response = await client.put(
        f"{DEVICES}/{device['id']}",
        json={"name": "Second edit"},
        headers={VERSION_HEADER: device["updated_at"]},
    )

    assert response.status_code == 409
    assert code(response) == "conflict"
    current = detail(response)["current"]
    assert current["id"] == device["id"]
    assert current["name"] == "First edit"


async def test_a_save_that_cannot_connect_is_reverted(
    client: AsyncClient, db: Database, services: Services
) -> None:
    """A wrong address must not leave the system unable to reach a working device."""
    await login(client)
    device = await create(client, driver_key="toggle", name="Matrix")
    await services.manager.wait_for_connection(device["id"])

    response = await client.put(
        f"{DEVICES}/{device['id']}",
        json={"config": {"transport": {"type": "loopback"}, "driver": {"answer": False}}},
        headers={VERSION_HEADER: device["updated_at"]},
    )

    assert response.status_code == 503
    assert code(response) == "device_unavailable"
    assert detail(response)["reverted"] is True

    row = await devices_crud.get(db, device["id"])
    assert row is not None
    assert row.config["driver"] == {}  # the previous configuration, unchanged
    assert row.name == "Matrix"
    record = await services.manager.wait_for_connection(device["id"])
    assert record is not None and record.status == "connected"  # still reachable


# -- DELETE ---------------------------------------------------------------------


async def test_delete_removes_the_device_and_its_state(
    client: AsyncClient, services: Services
) -> None:
    await login(client)
    device = await create(client)
    await services.manager.wait_for_connection(device["id"])

    response = await client.delete(f"{DEVICES}/{device['id']}")

    assert response.status_code == 204
    assert (await client.get(f"{DEVICES}/{device['id']}")).status_code == 404
    assert services.state.devices.record("hdmi") is None


async def test_deleting_an_unknown_device_is_not_found(client: AsyncClient) -> None:
    await login(client)

    response = await client.delete(f"{DEVICES}/404")

    assert response.status_code == 404
    assert code(response) == "not_found"


# -- test -----------------------------------------------------------------------


async def test_test_reports_the_two_stages_separately(client: AsyncClient) -> None:
    await login(client)
    good = await create(client, name="Good")
    silent = await create(
        client,
        driver_key="toggle",
        name="Silent",
        config={"transport": {"type": "loopback"}, "driver": {"answer": False}},
    )

    good_result = (await client.post(f"{DEVICES}/{good['id']}/test")).json()
    silent_result = (await client.post(f"{DEVICES}/{silent['id']}/test")).json()

    assert good_result["ok"] is True
    assert good_result["connect"]["ok"] is True and good_result["probe"]["ok"] is True
    assert silent_result["ok"] is False
    assert silent_result["connect"]["ok"] is True  # the transport opened
    assert silent_result["probe"]["ok"] is False  # the device did not reply
    assert "did not reply" in silent_result["message"]


async def test_testing_an_unknown_device_is_not_found(client: AsyncClient) -> None:
    await login(client)

    response = await client.post(f"{DEVICES}/404/test")

    assert response.status_code == 404
    assert code(response) == "not_found"


async def test_testing_a_running_mixer_never_opens_a_second_connection(
    client: AsyncClient, services: Services
) -> None:
    """§7.3 bench question 6: the CQ-20B refuses a second MIDI client, and a
    second connection attempt could knock the running one off. The test
    button must report the running driver's own status instead."""
    async with CqMidiStub() as stub:
        await login(client)
        created = await create(
            client,
            category="mixer",
            driver_key="cq20b",
            name="CQ-20B",
            config={
                "transport": {"type": "tcp", "host": "127.0.0.1", "port": stub.port},
                "driver": {"metering": False},
            },
        )
        record = await services.manager.wait_for_connection(created["id"])
        assert record is not None and record.status == "connected"
        assert stub.connections == 1

        response = await client.post(f"{DEVICES}/{created['id']}/test")

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["ok"] is True
        assert body["connect"]["attempted"] is False
        assert body["probe"]["attempted"] is False
        assert stub.connections == 1  # never opened a second connection


async def test_testing_a_disconnected_mixer_still_opens_one_connection(
    client: AsyncClient,
) -> None:
    """The "never a second connection" rule only applies while the mixer is
    already running (§7.3 bench question 6); a disconnected one is tested
    the ordinary way, exactly once."""
    async with CqMidiStub() as stub:
        pass  # closed before the device is even created: never connects

    await login(client)
    created = await create(
        client,
        category="mixer",
        driver_key="cq20b",
        name="CQ-20B",
        config={
            "transport": {"type": "tcp", "host": "127.0.0.1", "port": stub.port},
            "driver": {"metering": False},
        },
    )

    response = await client.post(f"{DEVICES}/{created['id']}/test")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is False
    assert body["connect"]["attempted"] is True


# -- the Main channel (§7.3) ------------------------------------------------------


async def test_creating_a_mixer_device_creates_its_main_channel(
    client: AsyncClient, db: Database
) -> None:
    await login(client)

    created = await create(client, category="mixer", driver_key="stub", name="Desk")

    channels = await mixer_crud.list_channels(db, device_id=created["id"])
    assert [c.channel_kind for c in channels] == ["main"]
    assert channels[0].name == "Main"
    refs = await mixer_crud.get_channel_refs(db, channels[0].id)
    assert [r.driver_ref for r in refs] == ["main"]


async def test_creating_a_non_mixer_device_never_creates_a_channel(
    client: AsyncClient, db: Database
) -> None:
    await login(client)

    created = await create(client, category="video_matrix", driver_key="stub", name="Matrix")

    assert await mixer_crud.list_channels(db, device_id=created["id"]) == []


# -- capabilities, refs, manifest, laws -----------------------------------------


async def test_capabilities_are_reported_as_connected_and_open_to_operators(
    client: AsyncClient, services: Services
) -> None:
    await login(client)
    device = await create(client)
    await services.manager.wait_for_connection(device["id"])
    await client.post(f"{AUTH}/logout")
    await login(client, OPERATOR_PASSWORD)

    response = await client.get(f"{DEVICES}/{device['id']}/capabilities")

    assert response.status_code == 200
    body = response.json()
    assert body["as_connected"] is True
    assert body["capabilities"]["input_count"] == 4


async def test_capabilities_of_an_unknown_device_are_not_found(client: AsyncClient) -> None:
    await login(client)

    response = await client.get(f"{DEVICES}/404/capabilities")

    assert response.status_code == 404
    assert code(response) == "not_found"


async def test_refs_list_the_drivers_references(client: AsyncClient) -> None:
    await login(client)
    device = await create(client)

    response = await client.get(f"{DEVICES}/{device['id']}/refs")

    assert response.status_code == 200
    body = response.json()
    assert [ref["label"] for ref in body["inputs"]] == [
        "Input 1",
        "Input 2",
        "Input 3",
        "Input 4",
    ]
    assert body["outputs"][0]["kind"] == "output"


async def test_refs_of_an_unknown_device_are_not_found(client: AsyncClient) -> None:
    await login(client)

    response = await client.get(f"{DEVICES}/404/refs")

    assert response.status_code == 404
    assert code(response) == "not_found"


async def test_manifest_is_served_for_a_surface_and_refused_for_a_matrix(
    client: AsyncClient,
) -> None:
    await login(client)
    surface = await create(
        client, category="control_surface", driver_key="testsurface", name="Desk"
    )
    matrix = await create(client, name="Matrix")

    served = await client.get(f"{DEVICES}/{surface['id']}/manifest")
    refused = await client.get(f"{DEVICES}/{matrix['id']}/manifest")

    assert served.status_code == 200
    assert served.json()["controls"][0]["id"] == "fader_1"
    assert refused.status_code == 404
    assert code(refused) == "not_found"


async def test_fader_law_is_served_for_a_mixer_and_refused_for_a_video_matrix(
    client: AsyncClient,
) -> None:
    await login(client)
    mixer = await create(client, category="mixer", driver_key="testmixer", name="Desk")
    matrix = await create(client, name="Matrix")

    served = await client.get(f"{DEVICES}/{mixer['id']}/fader-law")
    refused = await client.get(f"{DEVICES}/{matrix['id']}/fader-law")

    assert served.status_code == 200
    assert served.json()["fader_law"][0] == {
        "position": 0.0,
        "db": None,
        "label": "-inf",
        "detent": False,
    }
    assert refused.status_code == 404
    assert code(refused) == "not_found"


async def test_meter_scale_is_absent_where_metering_is(client: AsyncClient) -> None:
    """§5.5: where metering is unavailable the interface shows nothing at all."""
    await login(client)
    metered = await create(
        client,
        category="mixer",
        driver_key="testmixer",
        name="Metered",
        config={"transport": {"type": "loopback"}, "driver": {"metering": True}},
    )
    unmetered = await create(
        client,
        category="mixer",
        driver_key="testmixer",
        name="Unmetered",
        config={"transport": {"type": "loopback"}, "driver": {"metering": False}},
    )

    served = await client.get(f"{DEVICES}/{metered['id']}/meter-scale")
    refused = await client.get(f"{DEVICES}/{unmetered['id']}/meter-scale")

    assert served.status_code == 200
    assert served.json()["min_db"] == -60.0
    assert served.json()["meter_point"] == "post_fader"
    assert refused.status_code == 404
    assert code(refused) == "not_found"


# -- remap ----------------------------------------------------------------------


async def test_remap_reports_the_drivers_references(client: AsyncClient) -> None:
    await login(client)
    device = await create(client)

    listed = await client.get(f"{DEVICES}/{device['id']}/remap")
    applied = await client.post(f"{DEVICES}/{device['id']}/remap", json={"mappings": []})

    assert listed.status_code == 200
    assert listed.json()["mappings"] == []  # a matrix with no inputs or outputs yet
    assert len(listed.json()["available"]["inputs"]) == 4
    assert applied.status_code == 200


async def test_remap_rejects_a_mapping_that_is_not_a_list(client: AsyncClient) -> None:
    await login(client)
    device = await create(client)

    response = await client.post(f"{DEVICES}/{device['id']}/remap", json={"mappings": "all"})

    assert response.status_code == 422
    assert code(response) == "validation_failed"


# -- secrets (§6.10) ------------------------------------------------------------


async def test_a_password_is_never_returned_and_survives_a_save_without_it(
    client: AsyncClient, db: Database, services: Services
) -> None:
    await login(client)
    device = await create(
        client,
        category="mixer",
        driver_key="testmixer",
        name="Desk",
        config={"transport": {"type": "loopback"}, "driver": {"password": "hunter2"}},
    )

    assert device["config"]["driver"]["password"] == SECRET_SENTINEL
    listed = await client.get(DEVICES)
    one = await client.get(f"{DEVICES}/{device['id']}")
    assert "hunter2" not in listed.text
    assert "hunter2" not in one.text

    stored = await devices_crud.get(db, device["id"])
    assert stored is not None
    assert set(stored.config["driver"]["password"]) == {"enc"}

    updated = await client.put(
        f"{DEVICES}/{device['id']}",
        json={"config": {"transport": {"type": "loopback"}, "driver": {"metering": False}}},
        headers={VERSION_HEADER: device["updated_at"]},
    )

    assert updated.status_code == 200, updated.text
    after = await devices_crud.get(db, device["id"])
    assert after is not None
    plain = services.manager.decrypt_stored(MixerStubDriver, after.config)
    assert plain["driver"]["password"] == "hunter2"  # omitting it meant "unchanged"
    assert "hunter2" not in updated.text


# -- the firewall mirror (contracts §4, the Phase 1 gap) --------------------


def _tcp_config(host: str, port: int = 4352) -> dict[str, Any]:
    return {"transport": {"type": "tcp", "host": host, "port": port}, "driver": {}}


def firewall_devices(config: Config) -> list[dict[str, Any]]:
    """The entries the device table owns. The KNX gateway and the control
    surface are derived from their own sources beside them and are
    proved in tests/unit/core/test_system_config.py."""
    from proskenion.core import system_config

    doc = system_config.read(config.app.data_dir)
    devices = doc.get("devices")
    derived = {system_config.KNX_GATEWAY_NAME, system_config.CONTROL_SURFACE_NAME}
    if not isinstance(devices, list):
        return []
    return [d for d in devices if d.get("name") not in derived]


async def test_creating_a_device_mirrors_it_into_the_firewall(
    client: AsyncClient, config: Config
) -> None:
    await login(client)
    response = await client.post(
        DEVICES,
        json={
            "category": "projector",
            "driver_key": "tcptoggle",
            "name": "Lobby projector",
            "config": _tcp_config("10.2.30.249"),
        },
    )
    assert response.status_code == 201, response.text

    mirrored = firewall_devices(config)
    assert [d["address"] for d in mirrored] == ["10.2.30.249"]
    assert mirrored[0]["ports"] == ["tcp/4352"]


async def test_moving_a_device_to_a_new_address_updates_the_firewall(
    client: AsyncClient, config: Config
) -> None:
    """The Phase 1 gap: before this task the firewall table was static, so a
    device saved at a new address stayed firewalled by its old one. Saved
    disabled here so the PUT never waits on a connection attempt — the point
    of this test is the mirror, not the reconnect-and-test behaviour PUT
    already has its own tests for above.
    """
    await login(client)
    created = await client.post(
        DEVICES,
        json={
            "category": "projector",
            "driver_key": "tcptoggle",
            "name": "Lobby projector",
            "config": _tcp_config("10.2.30.249"),
            "enabled": False,
        },
    )
    assert created.status_code == 201, created.text
    device_id = created.json()["id"]
    assert [d["address"] for d in firewall_devices(config)] == ["10.2.30.249"]

    moved = await client.put(
        f"{DEVICES}/{device_id}",
        json={"config": _tcp_config("10.2.99.5"), "enabled": False},
        headers={VERSION_HEADER: created.json()["updated_at"]},
    )
    assert moved.status_code == 200, moved.text

    mirrored = firewall_devices(config)
    assert [d["address"] for d in mirrored] == ["10.2.99.5"]


async def test_a_reverted_save_re_mirrors_the_restored_address(
    client: AsyncClient, config: Config
) -> None:
    """A save that cannot reach the device is reverted (§16.7, §21.24,
    ``update_device``'s own docstring); the firewall mirror follows the
    revert too, or "revert" would leave the device firewalled by the
    address the save was undone from."""
    await login(client)
    created = await client.post(
        DEVICES,
        json={
            "category": "video_matrix",
            "driver_key": "toggle",
            "name": "Test matrix",
            "config": {**LOOPBACK, "driver": {"answer": True}},
        },
    )
    assert created.status_code == 201, created.text
    device_id = created.json()["id"]

    failing = await client.put(
        f"{DEVICES}/{device_id}",
        json={"config": {**LOOPBACK, "driver": {"answer": False}}},
        headers={VERSION_HEADER: created.json()["updated_at"]},
    )
    assert failing.status_code == 503
    assert failing.json()["error"]["detail"]["reverted"] is True
    # Both configurations are "loopback" (no address), so this proves only
    # that the mirror re-ran after the revert without erroring — the address
    # case is test_moving_a_device_to_a_new_address_updates_the_firewall.
    assert firewall_devices(config) == []


async def test_deleting_a_device_removes_it_from_the_firewall(
    client: AsyncClient, config: Config
) -> None:
    await login(client)
    created = await client.post(
        DEVICES,
        json={
            "category": "projector",
            "driver_key": "tcptoggle",
            "name": "Lobby projector",
            "config": _tcp_config("10.2.30.249"),
        },
    )
    device_id = created.json()["id"]
    assert firewall_devices(config) != []

    deleted = await client.delete(f"{DEVICES}/{device_id}")
    assert deleted.status_code == 204

    assert firewall_devices(config) == []


# -- pre-change snapshots on a driver/transport change (§7.2.4, §18) ------------------------


async def test_changing_a_devices_transport_takes_a_pre_change_snapshot(
    client: AsyncClient, config: Config
) -> None:
    """§7.2.4/§18: a device's transport is the one part of a save that a
    database snapshot cannot otherwise reconstruct once it is overwritten."""
    from proskenion.core.snapshots import snapshots_dir

    await login(client)
    created = await client.post(
        DEVICES,
        json={
            "category": "projector",
            "driver_key": "tcptoggle",
            "name": "Lobby projector",
            "config": _tcp_config("10.2.30.249"),
            "enabled": False,  # never waits on a connection attempt
        },
    )
    assert created.status_code == 201, created.text
    device_id = created.json()["id"]
    before = set(snapshots_dir(config.app.data_dir).glob("pre-change-*.db"))

    moved = await client.put(
        f"{DEVICES}/{device_id}",
        json={"config": _tcp_config("10.2.99.5"), "enabled": False},
        headers={VERSION_HEADER: created.json()["updated_at"]},
    )

    assert moved.status_code == 200, moved.text
    after = set(snapshots_dir(config.app.data_dir).glob("pre-change-*.db"))
    assert len(after) == len(before) + 1, "the transport change took no snapshot"


async def test_changing_a_devices_driver_takes_a_pre_change_snapshot(
    client: AsyncClient, config: Config
) -> None:
    from proskenion.core.snapshots import snapshots_dir

    await login(client)
    created = await client.post(
        DEVICES,
        json={
            "category": "video_matrix",
            "driver_key": "toggle",
            "name": "Test matrix",
            "config": LOOPBACK,
            "enabled": False,
        },
    )
    assert created.status_code == 201, created.text
    device_id = created.json()["id"]
    before = set(snapshots_dir(config.app.data_dir).glob("pre-change-*.db"))

    # "stub" is the other driver_key shipped for video_matrix in this test
    # module (see create()'s own default) — a genuinely different driver,
    # not a config tweak.
    switched = await client.put(
        f"{DEVICES}/{device_id}",
        json={"driver_key": "stub", "enabled": False},
        headers={VERSION_HEADER: created.json()["updated_at"]},
    )

    assert switched.status_code == 200, switched.text
    after = set(snapshots_dir(config.app.data_dir).glob("pre-change-*.db"))
    assert len(after) == len(before) + 1, "the driver change took no snapshot"


async def test_renaming_or_toggling_a_device_takes_no_snapshot(
    client: AsyncClient, config: Config, services: Services
) -> None:
    """The counterpart proof: §18 asks for pre-change snapshots on
    *destructive* actions, and a rename or an enable/disable toggle with the
    driver and transport untouched destroys nothing — unlike every ``DELETE``
    (§15.3's retention pool is shared, so a snapshot nobody needed is not
    free)."""
    from proskenion.core.snapshots import snapshots_dir

    await login(client)
    device = await create(client, driver_key="toggle", config=LOOPBACK)
    await services.manager.wait_for_connection(device["id"])
    before = set(snapshots_dir(config.app.data_dir).glob("pre-change-*.db"))

    renamed = await client.put(
        f"{DEVICES}/{device['id']}",
        json={"name": "Renamed only"},
        headers={VERSION_HEADER: device["updated_at"]},
    )
    assert renamed.status_code == 200, renamed.text

    toggled = await client.put(
        f"{DEVICES}/{device['id']}",
        json={"enabled": False},
        headers={VERSION_HEADER: renamed.json()["updated_at"]},
    )
    assert toggled.status_code == 200, toggled.text

    after = set(snapshots_dir(config.app.data_dir).glob("pre-change-*.db"))
    assert after == before, "a rename or a toggle alone took a snapshot"


# -- wiring (§12.1) -------------------------------------------------------------


async def test_the_lifespan_builds_starts_and_stops_the_device_manager(config: Config) -> None:
    application = create_app(config)

    async with application.router.lifespan_context(application):
        manager = application.state.devices
        assert isinstance(manager, DeviceManager)
        assert isinstance(application.state.state_store, StateStore)
        assert isinstance(application.state.bus, EventBus)
        assert application.state.state_store.owners("devices") == frozenset({OWNER})

    assert application.state.devices is None


async def test_requests_before_the_manager_is_running_are_device_unavailable(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    application = create_app(config, db=db, tokens=tokens, limiter=limiter)

    async with make_client(application) as http:
        await login(http)
        response = await http.get(DEVICES)

    assert response.status_code == 503
    assert code(response) == "device_unavailable"
