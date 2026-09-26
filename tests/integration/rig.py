"""A lighting rig commissioned through the API, against the knxd and Art-Net stubs.

The Phase 2 slice A milestone (``docs/plans/phase-2a.md``) is a sequence of
things done to one room: a bank of four DMX fixtures and a KNX house dimmer,
switched from a wall panel, reporting back on a panel indicator. Every clause
of it starts from the same room, built the way an installer builds it —
through the API, on a database that had never been commissioned:

1. §10.4's first-run wizard, so the rest runs under the admin session it issues
2. an ``artnet`` lighting output whose UDP transport points at the Art-Net stub,
   configured with ``POST /devices`` and waited on until it reports connected
3. the KNX address list ``tests/fixtures/knx/phase2a_milestone.csv``, imported
   through ``POST /knx/import`` (preview, then confirm)
4. four single-channel dimmer fixtures at DMX 1–4 and one KNX dimmer, patched
5. one lighting group, the bank, holding all five
6. the bank's binding rule on the panel's command address, on at 80 %
7. two derived statuses: the bank's indicator (every member at 80 %) and the
   external-control indicator

The stubs are the far end of real sockets: the application's own KNX
subsystem connects to the knxd stub over TCP, and its own ``artnet`` driver
sends ArtDmx to the Art-Net stub over UDP. What a clause asserts is what the
stubs recorded arriving — :attr:`KnxdStub.writes` and
:attr:`ArtNetStub.received` — never what the application says it sent. Both
stubs stamp each record with ``time.monotonic()`` on arrival, one clock shared
by the test process, so "the status telegram arrived after the frame" is a
comparison of two records rather than a guess about timing.

Slice B extends this room with a real wall panel and a visiting desk; the
builder is kept free of any one clause's assumptions so it can.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from httpx import AsyncClient, Response

from proskenion.api.app import API_PREFIX
from proskenion.core.dmx.compositor import level_to_dmx
from proskenion.core.lighting import LightingService
from tests.integration.test_first_run_flow import wait_for_status, walk_the_wizard
from tests.stubs.artnet_stub import ArtNetStub, ReceivedArtDmx
from tests.stubs.knxd_stub import KnxdStub, RecordedWrite

DEVICES = f"{API_PREFIX}/devices"
KNX = f"{API_PREFIX}/knx"
LIGHTING = f"{API_PREFIX}/lighting"
RULES = f"{API_PREFIX}/rules"
DERIVED = f"{API_PREFIX}/derived-status"
SCENES = f"{API_PREFIX}/scenes"

#: The integrator's address list for this room, in ETS's CSV export layout.
ADDRESS_LIST = Path(__file__).resolve().parents[1] / "fixtures" / "knx" / "phase2a_milestone.csv"

BANK_COMMAND = "1/0/1"
BANK_STATUS = "1/0/2"
EXTERNAL_CONTROL_STATUS = "1/0/9"
FOYER_SIGN = "1/3/1"
HOUSE_DIMMER = "2/1/1"

#: The wall panel's individual address, as the source of its telegrams.
PANEL = "1.1.20"
#: The controller's own individual address (§17.4), configured so the rule
#: layer can tell its own echoes from a panel press (§8.7).
CONTROLLER = "1.1.250"

#: Where the four fixtures are patched: universe 1, start addresses 1 to 4.
UNIVERSE = 1
FIXTURE_ADDRESSES = (1, 2, 3, 4)
#: §15.2's seeded "Single-channel dimmer" profile: one slot, role ``dimmer``.
#: Single-channel on purpose, so nothing here depends on how a fixture with
#: both a dimmer and colour channels is scaled (a question still open).
SINGLE_CHANNEL_DIMMER = 1

#: The bank's binding: on at 80 %, off at 0 %.
ON_LEVEL = 80.0
OFF_LEVEL = 0.0
#: 80 % as a DMX slot value, by the compositor's own conversion (§9.2).
ON_DMX = level_to_dmx(ON_LEVEL)

#: How long a record is waited for before the clause fails. Loopback sockets
#: and an in-memory database, so this guards against a hang rather than being
#: a delay — but it is set above the KNX subsystem's first reconnection delay
#: (``proskenion.core.knx.INITIAL_RETRY_DELAY_S``, 5 s), so that a write queued
#: while the subsystem reconnects to knxd is still seen, and a clause is decided
#: by what arrives rather than by when the reconnection happens to land.
AWAIT_S = 10.0
#: How often the stubs' records are looked at while waiting.
POLL_S = 0.005
#: How often the API is asked while waiting on something only it reports.
API_POLL_S = 0.02


async def until[T](probe: Callable[[], T | None], what: str, timeout_s: float = AWAIT_S) -> T:
    """Wait for ``probe`` to return something other than ``None`` or ``False``.

    ``probe`` reads a record — a stub's list of arrivals, the state store — so
    this waits for an event to have happened, not for time to pass.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while True:
        found = probe()
        if found is not None and found is not False:
            return found
        if loop.time() >= deadline:
            raise AssertionError(f"timed out after {timeout_s} s waiting for {what}")
        await asyncio.sleep(POLL_S)


async def eventually[T](
    probe: Callable[[], Awaitable[T | None]], what: str, timeout_s: float = AWAIT_S
) -> T:
    """:func:`until` for a record only the API can report — a log row written
    by a background task, say — asked for through ``probe``, a request."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while True:
        found = await probe()
        if found is not None and found is not False:
            return found
        if loop.time() >= deadline:
            raise AssertionError(f"timed out after {timeout_s} s waiting for {what}")
        await asyncio.sleep(API_POLL_S)


def ok(response: Response, status: int = 200) -> Any:
    """The body of a response that must have succeeded with ``status``."""
    assert response.status_code == status, f"{response.request.url}: {response.text}"
    return response.json() if response.content else None


#: The binding's debounce window (§8.4's default, 500 ms), with a margin.
PANEL_DEBOUNCE_S = 0.6


@dataclass
class Rig:
    """The commissioned room, the client signed in as admin, and both stubs."""

    client: AsyncClient
    app: FastAPI
    knxd: KnxdStub
    artnet: ArtNetStub
    output_id: int
    addresses: dict[str, int]
    fixtures: tuple[int, ...]
    dimmer: int
    bank: int
    binding: int
    bank_status: int
    external_status: int
    _last_press: float | None = field(default=None, repr=False)

    @property
    def lighting(self) -> LightingService:
        service: LightingService = self.app.state.lighting
        return service

    # -- marks: "everything that arrives from now on" -----------------------------

    def frame_mark(self) -> int:
        return len(self.artnet.received)

    def write_mark(self) -> int:
        return len(self.knxd.writes)

    # -- what the stubs recorded ------------------------------------------------------

    def frames(self, since: int = 0) -> list[ReceivedArtDmx]:
        """ArtDmx frames for the rig's universe that arrived at or after ``since``."""
        return [f for f in self.artnet.received[since:] if f.universe == UNIVERSE]

    def writes(self, group_address: str, since: int = 0) -> list[RecordedWrite]:
        """Group writes on ``group_address`` that arrived at or after ``since``."""
        return [w for w in self.knxd.writes[since:] if w.group_address == group_address]

    @staticmethod
    def fixture_values(frame: ReceivedArtDmx) -> tuple[int, ...]:
        """The four fixtures' slots in a frame, in patch order."""
        return tuple(frame.data[address - 1] for address in FIXTURE_ADDRESSES)

    async def first_frame(
        self, predicate: Callable[[tuple[int, ...]], bool], *, since: int, what: str
    ) -> ReceivedArtDmx:
        """The first frame from ``since`` on whose fixture slots satisfy ``predicate``."""

        def probe() -> ReceivedArtDmx | None:
            return next((f for f in self.frames(since) if predicate(self.fixture_values(f))), None)

        return await until(probe, what)

    async def first_write(
        self,
        group_address: str,
        dpt: str,
        predicate: Callable[[object], bool],
        *,
        since: int,
        what: str,
    ) -> RecordedWrite:
        """The first write on ``group_address`` from ``since`` on whose value satisfies it."""

        def probe() -> RecordedWrite | None:
            return next(
                (w for w in self.writes(group_address, since) if predicate(w.value(dpt))), None
            )

        return await until(probe, what)

    async def status_written(self, group_address: str, value: bool, *, since: int) -> RecordedWrite:
        """The first DPT 1.001 write of ``value`` to a status address from ``since`` on."""
        return await self.first_write(
            group_address,
            "1.001",
            lambda v: v is value,
            since=since,
            what=f"{group_address} written {int(value)}",
        )

    # -- the panel ------------------------------------------------------------------------

    async def press(self, value: bool) -> None:
        """The wall panel's bank button: a DPT 1.001 telegram on the command address.

        Two presses are kept at least one debounce window apart, as a person's
        would be: the rules engine ignores a repeat on the same rule inside its
        window (§8.4, default 500 ms), so back-to-back presses would test the
        debounce rather than the press.
        """
        if self._last_press is not None:
            wait = self._last_press + PANEL_DEBOUNCE_S - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
        await self.knxd.send_telegram(BANK_COMMAND, "1.001", value, source_address=PANEL)
        self._last_press = time.monotonic()

    async def bank_on(self) -> None:
        """Press on and wait until the room is up and the indicator says so."""
        frames, writes = self.frame_mark(), self.write_mark()
        await self.press(True)
        await self.first_frame(
            lambda values: values == (ON_DMX,) * 4, since=frames, what="the bank at its on level"
        )
        await self.status_written(BANK_STATUS, True, since=writes)

    # -- the API --------------------------------------------------------------------------

    async def lighting_state(self) -> dict[str, Any]:
        state: dict[str, Any] = ok(await self.client.get(f"{LIGHTING}/state"))
        return state

    async def set_external_control(self, manual: bool) -> dict[str, Any]:
        body: dict[str, Any] = ok(
            await self.client.post(f"{LIGHTING}/external-control", json={"manual": manual})
        )
        return body

    async def rule_logged(self, rule_id: int, result: str) -> dict[str, Any]:
        """The rule's newest execution-log entry, once it reads ``result``.

        §8.10's log is written by a background task, so a firing reaches it
        a moment after it happens.
        """

        async def probe() -> dict[str, Any] | None:
            log = ok(await self.client.get(f"{RULES}/log", params={"rule_id": rule_id}))
            entries: list[dict[str, Any]] = log["entries"]
            return entries[0] if entries and entries[0]["result"] == result else None

        return await eventually(probe, f"rule {rule_id} logged {result!r}")

    async def completed_run(self, scene_id: int) -> dict[str, Any]:
        """The scene's newest execution-log entry, once the run has completed.

        §8.15: a scene completes when its fades have finished, so this is also
        the point at which its look is final.
        """

        async def probe() -> dict[str, Any] | None:
            log = ok(await self.client.get(f"{SCENES}/{scene_id}/log"))
            entries: list[dict[str, Any]] = log["entries"]
            return entries[0] if entries and entries[0]["completed_at"] is not None else None

        return await eventually(probe, f"scene {scene_id} to complete")


async def build_rig(client: AsyncClient, app: FastAPI, knxd: KnxdStub, artnet: ArtNetStub) -> Rig:
    """Commission a fresh appliance and configure the room — see the module docstring."""
    await walk_the_wizard(client)  # leaves the client signed in as admin

    # -- the lighting output: the real artnet driver, aimed at the stub node -------
    output = ok(
        await client.post(
            DEVICES,
            json={
                "category": "lighting_output",
                "driver_key": "artnet",
                "name": "eDMX8 MAX",
                "config": {
                    "transport": {"type": "udp", "host": "127.0.0.1", "port": artnet.port},
                    "driver": {},
                },
            },
        ),
        201,
    )
    await wait_for_status(client, output["id"], "connected")

    # -- the address list, imported the way §21.19's wizard does it --------------
    preview = ok(
        await client.post(
            f"{KNX}/import",
            data={"step": "preview"},
            files={"file": (ADDRESS_LIST.name, ADDRESS_LIST.read_bytes(), "text/csv")},
        )
    )
    assert preview["format"] == "ets_csv", preview
    assert preview["importable_count"] == 5, preview
    confirmed = ok(
        await client.post(
            f"{KNX}/import",
            data={"step": "confirm", "token": preview["token"], "direction": "both"},
        )
    )
    assert confirmed["added_count"] == 5, confirmed
    library = ok(await client.get(f"{KNX}/addresses"))
    addresses = {row["group_address"]: row["id"] for row in library}

    # -- the patch: four DMX fixtures and a KNX house dimmer ----------------------
    fixtures: list[int] = []
    for number, address in enumerate(FIXTURE_ADDRESSES, start=1):
        fixture = ok(
            await client.post(
                f"{LIGHTING}/channels",
                json={
                    "name": f"Bank 1 fixture {number}",
                    "type": "dmx",
                    "profile_id": SINGLE_CHANNEL_DIMMER,
                    "device_id": output["id"],
                    "universe": UNIVERSE,
                    "address": address,
                },
            ),
            201,
        )
        assert fixture["conflicts"] == [], fixture
        fixtures.append(fixture["id"])
    dimmer = ok(
        await client.post(
            f"{LIGHTING}/channels",
            json={
                "name": "House centre",
                "type": "knx_dimmer",
                "knx_command_address_id": addresses[HOUSE_DIMMER],
                "fade_mode": "hardware",
            },
        ),
        201,
    )

    # -- the bank, its binding and its derived statuses ----------------------------
    members = [*fixtures, dimmer["id"]]
    bank = ok(
        await client.post(
            f"{LIGHTING}/groups", json={"name": "Stage bank 1", "channel_ids": members}
        ),
        201,
    )
    binding = ok(
        await client.post(
            RULES,
            json={
                "name": "Stage bank 1",
                "trigger_type": "knx",
                "knx_address_id": addresses[BANK_COMMAND],
                "match_type": "any",
                "action_type": "lighting_group",
                "lighting_group_id": bank["id"],
                "on_level": ON_LEVEL,
                "off_level": OFF_LEVEL,
            },
        ),
        201,
    )
    assert binding["fires_automatically"] is True, binding
    bank_status = ok(
        await client.post(
            DERIVED,
            json={
                "name": "Stage bank 1 indicator",
                "knx_address_id": addresses[BANK_STATUS],
                "source_type": "lighting_group_all_at",
                "lighting_group_id": bank["id"],
                "compare_level": ON_LEVEL,
            },
        ),
        201,
    )
    external_status = ok(
        await client.post(
            DERIVED,
            json={
                "name": "External control indicator",
                "knx_address_id": addresses[EXTERNAL_CONTROL_STATUS],
                "source_type": "external_control",
            },
        ),
        201,
    )

    rig = Rig(
        client=client,
        app=app,
        knxd=knxd,
        artnet=artnet,
        output_id=output["id"],
        addresses=addresses,
        fixtures=tuple(fixtures),
        dimmer=dimmer["id"],
        bank=bank["id"],
        binding=binding["id"],
        bank_status=bank_status["id"],
        external_status=external_status["id"],
    )

    # Every configuration write emits LightingConfigChanged, and the lighting
    # service reloads on receipt, off the request path. A panel press is
    # seconds away in a real room; here it is the next line, so wait for the
    # reload to have happened rather than race it.
    await until(
        lambda: rig.lighting.config.groups.get(rig.bank) == frozenset(members),
        "the lighting service to load the bank",
    )
    # Statuses are never persisted and are written once when first
    # configured (§12.1): the panel starts in line with the room — bank off,
    # no desk.
    await rig.status_written(BANK_STATUS, False, since=0)
    await rig.status_written(EXTERNAL_CONTROL_STATUS, False, since=0)
    return rig
