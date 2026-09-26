"""The KNX library, import wizard and monitor endpoints (§7.1, §16.7, §21.19).

Every endpoint has a success test and one covering its primary failure mode,
asserting the §16.1 envelope code, not merely the status (§22.4).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, Response

from proskenion.api.app import API_PREFIX, create_app
from proskenion.api.knx import API_MAX_UPLOAD_BYTES
from proskenion.api.knx import monitor as monitor_endpoint
from proskenion.config import Config, KnxSection
from proskenion.core.auth import TokenClaims, TokenService
from proskenion.core.bus import EventBus
from proskenion.core.devices import DeviceManager
from proskenion.core.events import LightingConfigChanged
from proskenion.core.knx import KnxSubsystem
from proskenion.core.knx_registry import DbAddressRegistry
from proskenion.core.ratelimit import RateLimiter
from proskenion.core.state import StateStore
from proskenion.db.connection import Database
from proskenion.db.crud import knx as knx_crud
from proskenion.db.crud import rules as rules_crud
from tests.stubs.knxd_stub import KnxdStub
from tests.unit.api.conftest import ADMIN_PASSWORD, OPERATOR_PASSWORD, make_client

KNX = f"{API_PREFIX}/knx"
AUTH = f"{API_PREFIX}/auth"

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "knx"


def _read(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


# -- fixtures: a real KnxSubsystem over the stub, injected like devices_manager -----


@dataclass
class Services:
    bus: EventBus
    state: StateStore
    manager: DeviceManager
    registry: DbAddressRegistry
    stub: KnxdStub
    knx: KnxSubsystem
    task: asyncio.Task[None]


async def _wait_for_status(
    state: StateStore, key: str, status: str, timeout_s: float = 2.0
) -> None:
    async def poll() -> None:
        while True:
            record = state.devices.record(key)
            if record is not None and record.status == status:
                return
            await asyncio.sleep(0.01)

    await asyncio.wait_for(poll(), timeout_s)


@pytest.fixture
async def services(db: Database, config: Config) -> AsyncIterator[Services]:
    bus = EventBus()
    await bus.start()
    state = StateStore(config, bus)
    manager = DeviceManager(db, state, bus, config)
    await manager.start()
    registry = DbAddressRegistry(db)
    await registry.reload()
    async with KnxdStub() as stub:
        knx_cfg = KnxSection(host="127.0.0.1", port=stub.port)
        knx = KnxSubsystem(
            knx_cfg,
            registry,
            bus,
            manager.report_subsystem_status,
            backoff_initial_s=0.02,
            backoff_max_s=0.05,
        )
        task = asyncio.create_task(knx.run())
        await _wait_for_status(state, "knx", "connected")
        try:
            yield Services(bus, state, manager, registry, stub, knx, task)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
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
        knx=services.knx,
        knx_registry=services.registry,
    )


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with make_client(app) as http:
        yield http


# -- helpers ------------------------------------------------------------------------


async def login(client: AsyncClient, password: str = ADMIN_PASSWORD) -> Response:
    return await client.post(f"{AUTH}/login", json={"password": password})


def code(response: Response) -> str:
    body: dict[str, Any] = response.json()
    return str(body["error"]["code"])


def detail(response: Response) -> dict[str, Any]:
    body: dict[str, Any] = response.json()
    return dict(body["error"]["detail"])


def _multipart_fields(fields: Mapping[str, str]) -> dict[str, tuple[None, str]]:
    """Plain form fields, forced into multipart encoding (no filename)."""
    return {k: (None, v) for k, v in fields.items()}


async def preview(
    client: AsyncClient,
    content: bytes,
    filename: str,
    *,
    fmt: str | None = None,
    mapping: dict[str, str] | None = None,
) -> Response:
    payload: dict[str, str] = {"step": "preview"}
    if fmt is not None:
        payload["format"] = fmt
    if mapping is not None:
        payload["mapping"] = json.dumps(mapping)
    files: dict[str, Any] = {**_multipart_fields(payload), "file": (filename, content, "text/csv")}
    return await client.post(f"{KNX}/import", files=files)


async def confirm(
    client: AsyncClient, token: str, *, direction: str = "incoming", strategy: str = "skip"
) -> Response:
    payload = {
        "step": "confirm",
        "token": token,
        "direction": direction,
        "duplicate_strategy": strategy,
    }
    return await client.post(f"{KNX}/import", files=_multipart_fields(payload))


async def create_address(
    client: AsyncClient,
    *,
    group_address: str = "1/0/1",
    name: str = "Stage Lights Command",
    dpt: str = "1.001",
    direction: str = "incoming",
    **extra: Any,
) -> Response:
    body = {
        "group_address": group_address,
        "name": name,
        "dpt": dpt,
        "direction": direction,
        **extra,
    }
    return await client.post(f"{KNX}/addresses", json=body)


# -- addresses: CRUD ------------------------------------------------------------------


class TestAddressCrud:
    async def test_create_and_list(self, client: AsyncClient) -> None:
        await login(client)
        response = await create_address(client)
        assert response.status_code == 201
        body = response.json()
        assert body["group_address"] == "1/0/1"
        assert body["used_count"] == 0

        listing = await client.get(f"{KNX}/addresses")
        assert listing.status_code == 200
        assert [a["group_address"] for a in listing.json()] == ["1/0/1"]

    async def test_library_edits_tell_lighting_and_rules(
        self, client: AsyncClient, services: Services
    ) -> None:
        # Dimmer channels and rules resolve address ids to group addresses
        # and data types; both reload on LightingConfigChanged.
        seen: list[str | None] = []

        async def on_change(event: LightingConfigChanged) -> None:
            seen.append(event.reason)

        services.bus.subscribe(LightingConfigChanged, on_change, name="test:config")
        await login(client)
        created = await create_address(client)
        assert created.status_code == 201
        deleted = await client.delete(f"{KNX}/addresses/{created.json()['id']}")
        assert deleted.status_code == 204
        for _ in range(50):
            if len(seen) == 2:
                break
            await asyncio.sleep(0.01)
        assert seen == ["knx_library", "knx_library"]

    async def test_create_rejects_malformed_group_address(self, client: AsyncClient) -> None:
        await login(client)
        response = await create_address(client, group_address="not-an-address")
        assert response.status_code == 422
        assert code(response) == "validation_failed"
        assert "group_address" in detail(response)

    async def test_create_rejects_unsupported_dpt(self, client: AsyncClient) -> None:
        await login(client)
        response = await create_address(client, dpt="99.999")
        assert response.status_code == 422
        assert code(response) == "validation_failed"
        assert "dpt" in detail(response)

    async def test_create_rejects_duplicate_group_address(self, client: AsyncClient) -> None:
        await login(client)
        await create_address(client)
        response = await create_address(client, name="Different Name")
        assert response.status_code == 422
        assert code(response) == "validation_failed"

    async def test_operator_cannot_create(self, client: AsyncClient) -> None:
        await login(client, OPERATOR_PASSWORD)
        response = await create_address(client)
        assert response.status_code == 403
        assert code(response) == "permission_denied"

    async def test_get_by_id(self, client: AsyncClient) -> None:
        await login(client)
        created = (await create_address(client)).json()
        response = await client.get(f"{KNX}/addresses/{created['id']}")
        assert response.status_code == 200
        assert response.json()["group_address"] == "1/0/1"

    async def test_get_missing_is_not_found(self, client: AsyncClient) -> None:
        await login(client)
        response = await client.get(f"{KNX}/addresses/999")
        assert response.status_code == 404
        assert code(response) == "not_found"

    async def test_update_with_correct_version(self, client: AsyncClient) -> None:
        await login(client)
        created = (await create_address(client)).json()
        response = await client.put(
            f"{KNX}/addresses/{created['id']}",
            json={"name": "Renamed"},
            headers={"If-Unmodified-Since-Version": created["updated_at"]},
        )
        assert response.status_code == 200
        assert response.json()["name"] == "Renamed"

    async def test_update_with_stale_version_is_conflict_with_current(
        self, client: AsyncClient
    ) -> None:
        await login(client)
        created = (await create_address(client)).json()
        await client.put(
            f"{KNX}/addresses/{created['id']}",
            json={"name": "First Edit"},
            headers={"If-Unmodified-Since-Version": created["updated_at"]},
        )
        stale = await client.put(
            f"{KNX}/addresses/{created['id']}",
            json={"name": "Second Edit"},
            headers={"If-Unmodified-Since-Version": created["updated_at"]},
        )
        assert stale.status_code == 409
        assert code(stale) == "conflict"
        assert detail(stale)["current"]["name"] == "First Edit"

    async def test_update_without_version_header_is_validation_failed(
        self, client: AsyncClient
    ) -> None:
        await login(client)
        created = (await create_address(client)).json()
        response = await client.put(f"{KNX}/addresses/{created['id']}", json={"name": "X"})
        assert response.status_code == 422
        assert code(response) == "validation_failed"

    async def test_delete_unreferenced_address(self, client: AsyncClient) -> None:
        await login(client)
        created = (await create_address(client)).json()
        response = await client.delete(f"{KNX}/addresses/{created['id']}")
        assert response.status_code == 204
        assert (await client.get(f"{KNX}/addresses/{created['id']}")).status_code == 404

    async def test_delete_referenced_address_is_in_use_with_the_rule_listed(
        self, client: AsyncClient, db: Database
    ) -> None:
        await login(client)
        created = (await create_address(client)).json()
        rule = await rules_crud.create_rule(
            db,
            name="Alarm Notify",
            trigger_type="knx",
            action_type="notify",
            knx_address_id=created["id"],
            message="Alarm zone armed",
        )

        listing = await client.get(f"{KNX}/addresses")
        assert listing.json()[0]["used_count"] == 1

        response = await client.delete(f"{KNX}/addresses/{created['id']}")
        assert response.status_code == 409
        assert code(response) == "in_use"
        references = detail(response)["references"]
        assert any(r["entity"] == "rules" and r["id"] == rule.id for r in references)

    async def test_references_endpoint(self, client: AsyncClient, db: Database) -> None:
        await login(client)
        created = (await create_address(client)).json()
        await rules_crud.create_rule(
            db,
            name="Alarm Notify",
            trigger_type="knx",
            action_type="notify",
            knx_address_id=created["id"],
            message="Alarm zone armed",
        )
        response = await client.get(f"{KNX}/addresses/{created['id']}/references")
        assert response.status_code == 200
        assert response.json()[0]["entity"] == "rules"

    async def test_filter_by_direction(self, client: AsyncClient) -> None:
        await login(client)
        await create_address(client, group_address="1/0/1", direction="incoming")
        await create_address(client, group_address="1/0/2", direction="outgoing")
        response = await client.get(f"{KNX}/addresses", params={"direction": "outgoing"})
        assert [a["group_address"] for a in response.json()] == ["1/0/2"]

    async def test_new_address_is_decoded_without_a_restart(
        self, client: AsyncClient, services: Services
    ) -> None:
        """Scope item 1: create an address, then a telegram on it is decoded —
        no restart, no re-fetching the app, only the registry reload the
        create endpoint already performs."""
        await login(client)
        response = await create_address(
            client, group_address="6/0/1", dpt="1.001", direction="incoming"
        )
        assert response.status_code == 201

        await services.stub.send_telegram("6/0/1", "1.001", True)
        await asyncio.sleep(0.1)  # let the read loop decode it
        entries = services.knx.monitor.recent()
        assert any(e.group_address == "6/0/1" and e.value is True for e in entries)


# -- device groups ----------------------------------------------------------------------


class TestDeviceGroupCrud:
    async def test_create_and_list(self, client: AsyncClient) -> None:
        await login(client)
        response = await client.post(f"{KNX}/device-groups", json={"name": "Foyer"})
        assert response.status_code == 201
        listing = await client.get(f"{KNX}/device-groups")
        assert [g["name"] for g in listing.json()] == ["Foyer"]

    async def test_create_requires_name(self, client: AsyncClient) -> None:
        await login(client)
        response = await client.post(f"{KNX}/device-groups", json={"name": ""})
        assert response.status_code == 422
        assert code(response) == "validation_failed"

    async def test_update_conflict(self, client: AsyncClient) -> None:
        await login(client)
        created = (await client.post(f"{KNX}/device-groups", json={"name": "Foyer"})).json()
        stale = await client.put(
            f"{KNX}/device-groups/{created['id']}",
            json={"name": "Renamed"},
            headers={"If-Unmodified-Since-Version": "1999-01-01T00:00:00+12:00"},
        )
        assert stale.status_code == 409
        assert code(stale) == "conflict"

    async def test_delete(self, client: AsyncClient) -> None:
        await login(client)
        created = (await client.post(f"{KNX}/device-groups", json={"name": "Foyer"})).json()
        response = await client.delete(f"{KNX}/device-groups/{created['id']}")
        assert response.status_code == 204

    async def test_delete_missing_is_not_found(self, client: AsyncClient) -> None:
        await login(client)
        response = await client.delete(f"{KNX}/device-groups/999")
        assert response.status_code == 404
        assert code(response) == "not_found"

    async def test_references(self, client: AsyncClient) -> None:
        await login(client)
        group = (await client.post(f"{KNX}/device-groups", json={"name": "Foyer"})).json()
        await create_address(client, device_id=group["id"])
        response = await client.get(f"{KNX}/device-groups/{group['id']}/references")
        assert response.status_code == 200
        assert response.json()[0]["entity"] == "knx_group_addresses"

    async def test_delete_refused_while_addresses_are_assigned(
        self, client: AsyncClient
    ) -> None:
        """§21.19: "Deletion is allowed only when no addresses are assigned.\""""
        await login(client)
        group = (await client.post(f"{KNX}/device-groups", json={"name": "Foyer"})).json()
        address = (await create_address(client, device_id=group["id"])).json()

        response = await client.delete(f"{KNX}/device-groups/{group['id']}")
        assert response.status_code == 409
        assert code(response) == "in_use"
        references = detail(response)["references"]
        assert any(
            r["entity"] == "knx_group_addresses" and r["id"] == address["id"] for r in references
        )
        # Refused, not deleted.
        assert (await client.get(f"{KNX}/device-groups/{group['id']}")).status_code == 200

    async def test_delete_allowed_once_addresses_are_reassigned(
        self, client: AsyncClient
    ) -> None:
        await login(client)
        foyer = (await client.post(f"{KNX}/device-groups", json={"name": "Foyer"})).json()
        stage = (await client.post(f"{KNX}/device-groups", json={"name": "Stage"})).json()
        address = (await create_address(client, device_id=foyer["id"])).json()
        assert (await client.delete(f"{KNX}/device-groups/{foyer['id']}")).status_code == 409

        # Reassign the address to a different group, then the delete succeeds.
        reassigned = await client.put(
            f"{KNX}/addresses/{address['id']}",
            json={"device_id": stage["id"]},
            headers={"If-Unmodified-Since-Version": address["updated_at"]},
        )
        assert reassigned.status_code == 200
        assert reassigned.json()["device_id"] == stage["id"]

        response = await client.delete(f"{KNX}/device-groups/{foyer['id']}")
        assert response.status_code == 204
        assert (await client.get(f"{KNX}/device-groups/{foyer['id']}")).status_code == 404


# -- import: three formats ---------------------------------------------------------------


class TestImportFormats:
    async def test_preview_shows_a_malformed_row_with_a_warning_not_dropped(
        self, client: AsyncClient
    ) -> None:
        await login(client)
        response = await preview(client, _read("ets_export.csv"), "ets_export.csv")
        assert response.status_code == 200
        body = response.json()
        assert body["row_count"] == 5
        malformed = next(r for r in body["rows"] if r["name"] == "Malformed Row")
        assert malformed["importable"] is False
        assert any(w["code"] == "malformed" for w in malformed["warnings"])

    async def test_ets_csv_imports_from_its_fixture(self, client: AsyncClient) -> None:
        await login(client)
        preview_response = await preview(client, _read("ets_export.csv"), "ets_export.csv")
        token = preview_response.json()["token"]
        confirm_response = await confirm(client, token)
        assert confirm_response.status_code == 200
        body = confirm_response.json()
        assert set(body["added"]) == {"1/0/1", "1/0/2", "1/1/1", "2/1/1"}

        listing = await client.get(f"{KNX}/addresses")
        assert {a["group_address"] for a in listing.json()} == {"1/0/1", "1/0/2", "1/1/1", "2/1/1"}

    async def test_ets_tsv_imports_from_its_fixture(self, client: AsyncClient) -> None:
        await login(client)
        preview_response = await preview(client, _read("ets_export.tsv"), "ets_export.tsv")
        token = preview_response.json()["token"]
        confirm_response = await confirm(client, token, direction="outgoing")
        assert confirm_response.status_code == 200
        assert set(confirm_response.json()["added"]) == {"3/0/1", "3/0/2"}

    async def test_ets_xml_imports_from_its_fixture(self, client: AsyncClient) -> None:
        await login(client)
        preview_response = await preview(client, _read("ets_export.xml"), "ets_export.xml")
        assert preview_response.status_code == 200
        token = preview_response.json()["token"]
        confirm_response = await confirm(client, token)
        assert confirm_response.status_code == 200
        assert set(confirm_response.json()["added"]) == {"4/0/1", "4/0/2"}

    async def test_generic_csv_imports_with_a_mapping(self, client: AsyncClient) -> None:
        await login(client)
        mapping = {"GA": "group_address", "Nm": "name", "Ty": "dpt"}
        preview_response = await preview(
            client, _read("generic.csv"), "generic.csv", fmt="generic", mapping=mapping
        )
        assert preview_response.status_code == 200
        token = preview_response.json()["token"]
        confirm_response = await confirm(client, token)
        assert confirm_response.status_code == 200
        assert set(confirm_response.json()["added"]) == {"5/0/1", "5/0/2"}

    async def test_esf_upload_is_refused_with_a_clear_message(self, client: AsyncClient) -> None:
        await login(client)
        response = await preview(client, _read("legacy.esf"), "legacy.esf")
        assert response.status_code == 422
        assert code(response) == "validation_failed"
        assert "esf" in response.json()["error"]["message"].lower()
        assert detail(response)["reason"] == "esf_not_supported"

    async def test_xml_with_external_entity_is_refused(self, client: AsyncClient) -> None:
        await login(client)
        response = await preview(client, _read("xxe.xml"), "xxe.xml")
        assert response.status_code == 422
        assert code(response) == "validation_failed"
        assert detail(response)["reason"] == "unsafe_xml"

    async def test_upload_over_the_size_limit_is_refused(self, client: AsyncClient) -> None:
        await login(client)
        oversized = b"x" * (API_MAX_UPLOAD_BYTES + 1)
        response = await preview(client, oversized, "big.csv")
        assert response.status_code == 422
        assert code(response) == "validation_failed"
        assert detail(response)["file"] == ["too_large"]

    async def test_export_re_imports_to_an_identical_library(self, client: AsyncClient) -> None:
        await login(client)
        token = (await preview(client, _read("ets_export.csv"), "ets_export.csv")).json()["token"]
        await confirm(client, token)
        original = sorted(
            (a["group_address"], a["name"], a["dpt"])
            for a in (await client.get(f"{KNX}/addresses")).json()
        )

        exported = await client.get(f"{KNX}/export")
        assert exported.status_code == 200
        assert exported.headers["content-type"].startswith("text/csv")

        # Delete everything, then re-import the export.
        for row in (await client.get(f"{KNX}/addresses")).json():
            await client.delete(f"{KNX}/addresses/{row['id']}")

        reimport_token = (
            await preview(client, exported.content, "knx-group-addresses.csv")
        ).json()["token"]
        await confirm(client, reimport_token)
        roundtripped = sorted(
            (a["group_address"], a["name"], a["dpt"])
            for a in (await client.get(f"{KNX}/addresses")).json()
        )
        assert roundtripped == original

    async def test_duplicate_strategy_skip(self, client: AsyncClient) -> None:
        await login(client)
        await create_address(client, group_address="1/0/1", name="Original")
        token = (await preview(client, _read("ets_export.csv"), "ets_export.csv")).json()["token"]
        response = await confirm(client, token, strategy="skip")
        assert "1/0/1" in response.json()["skipped"]
        address = await client.get(f"{KNX}/addresses/1")
        assert address.json()["name"] == "Original"

    async def test_duplicate_strategy_overwrite(self, client: AsyncClient) -> None:
        await login(client)
        await create_address(client, group_address="1/0/1", name="Original")
        token = (await preview(client, _read("ets_export.csv"), "ets_export.csv")).json()["token"]
        response = await confirm(client, token, strategy="overwrite")
        assert "1/0/1" in response.json()["updated"]
        address = await client.get(f"{KNX}/addresses/1")
        assert address.json()["name"] == "Stage Lights Command"

    async def test_confirm_with_unknown_token_is_not_found(self, client: AsyncClient) -> None:
        await login(client)
        response = await confirm(client, "does-not-exist")
        assert response.status_code == 404
        assert code(response) == "not_found"

    async def test_operator_cannot_import(self, client: AsyncClient) -> None:
        await login(client, OPERATOR_PASSWORD)
        response = await preview(client, _read("ets_export.csv"), "ets_export.csv")
        assert response.status_code == 403
        assert code(response) == "permission_denied"


# -- test-write ---------------------------------------------------------------------------


class TestTestWrite:
    async def test_write_to_outgoing_address_succeeds(
        self, client: AsyncClient, services: Services
    ) -> None:
        await login(client)
        created = (
            await create_address(client, group_address="1/0/9", direction="outgoing")
        ).json()
        response = await client.post(
            f"{KNX}/addresses/{created['id']}/test-write", json={"value": True}
        )
        assert response.status_code == 200
        assert response.json()["ok"] is True
        await asyncio.sleep(0.05)
        assert any(w.group_address == "1/0/9" for w in services.stub.writes)

    async def test_write_to_incoming_only_address_is_validation_failed(
        self, client: AsyncClient
    ) -> None:
        await login(client)
        created = (
            await create_address(client, group_address="1/0/9", direction="incoming")
        ).json()
        response = await client.post(
            f"{KNX}/addresses/{created['id']}/test-write", json={"value": True}
        )
        assert response.status_code == 422
        assert code(response) == "validation_failed"
        assert detail(response)["reason"] == "incoming_only"


# -- monitor (SSE) --------------------------------------------------------------------------


class TestMonitor:
    async def test_monitor_streams_a_telegram_from_the_stub(
        self, services: Services, db: Database
    ) -> None:
        """§21.19's live monitor, over SSE.

        Driven by calling the route function directly rather than through an
        HTTP client: ``httpx.ASGITransport`` runs the whole ASGI application
        call to completion before handing back a response (confirmed by
        direct experiment against a minimal SSE endpoint), which is fine for
        an ordinary request but means it never returns *anything* for a
        never-ending stream — the test would hang forever, not fail. Calling
        :func:`proskenion.api.knx.monitor` directly and driving its
        ``StreamingResponse.body_iterator`` by hand exercises exactly the
        same production code (route handler, :func:`_monitor_entry_dict`,
        the SSE framing) without going through a transport that cannot
        represent an open-ended stream.
        """
        await knx_crud.create_address(
            db, group_address="1/0/1", name="Stage", dpt="1.001", direction="incoming"
        )
        await services.registry.reload()

        claims = TokenClaims(
            tier="admin",
            token_version=1,
            issued_at=datetime.now(tz=UTC),
            expires_at=datetime.now(tz=UTC) + timedelta(hours=1),
            absolute_expires_at=datetime.now(tz=UTC) + timedelta(hours=12),
        )
        response = await monitor_endpoint(claims, services.knx)
        assert response.media_type == "text/event-stream"
        iterator = response.body_iterator

        await services.stub.send_telegram("1/0/1", "1.001", True)
        chunk = await asyncio.wait_for(anext(iterator), timeout=2.0)
        raw = chunk if isinstance(chunk, bytes) else chunk.encode()
        line = raw.decode().strip()
        assert line.startswith("data: ")
        event = json.loads(line[len("data: ") :])
        assert event["group_address"] == "1/0/1"
        assert event["value"] is True
        assert event["direction"] == "incoming"

        aclose = getattr(iterator, "aclose", None)
        if aclose is not None:
            await aclose()

    async def test_unauthenticated_is_refused(self, client: AsyncClient) -> None:
        response = await client.get(f"{KNX}/monitor")
        assert response.status_code == 401
        assert code(response) == "unauthenticated"


# -- §22.4 audit: failure tests added ----
#
# Every endpoint above already carried a success test and a domain failure
# test (one asserting the §16.1 envelope's code, not only the HTTP status)
# before this audit, with two exceptions noted by the classes below: GET
# /knx/device-groups/{group_id} and PUT /knx/device-groups/{group_id} had no
# success test either, only the PUT's stale-version failure. The tests
# collected here close every remaining gap.


class TestAddressesAudit:
    """GET /knx/addresses and GET /knx/addresses/{id}/references had no
    failure test before this audit."""

    async def test_list_rejects_an_unknown_direction_filter(self, client: AsyncClient) -> None:
        """``direction`` is an enum query parameter, so an out-of-vocabulary
        value fails FastAPI's own request validation before the handler
        ever runs."""
        await login(client)
        response = await client.get(f"{KNX}/addresses", params={"direction": "sideways"})
        assert response.status_code == 422
        assert code(response) == "validation_failed"

    async def test_references_for_an_unknown_address_is_not_found(
        self, client: AsyncClient
    ) -> None:
        await login(client)
        response = await client.get(f"{KNX}/addresses/999/references")
        assert response.status_code == 404
        assert code(response) == "not_found"


class TestDeviceGroupsAudit:
    """GET /knx/device-groups, GET .../{group_id} and GET
    .../{group_id}/references had no failure test before this audit; GET
    .../{group_id} and PUT .../{group_id} had no success test either."""

    async def test_operator_cannot_list_device_groups(self, client: AsyncClient) -> None:
        """A plain listing has no domain failure mode of its own, so the
        tier refusal is the only failure this endpoint can produce (§22.4's
        allowance for a parameterless GET)."""
        await login(client, OPERATOR_PASSWORD)
        response = await client.get(f"{KNX}/device-groups")
        assert response.status_code == 403
        assert code(response) == "permission_denied"

    async def test_get_by_id(self, client: AsyncClient) -> None:
        await login(client)
        created = (await client.post(f"{KNX}/device-groups", json={"name": "Foyer"})).json()
        response = await client.get(f"{KNX}/device-groups/{created['id']}")
        assert response.status_code == 200
        assert response.json()["name"] == "Foyer"

    async def test_get_missing_is_not_found(self, client: AsyncClient) -> None:
        await login(client)
        response = await client.get(f"{KNX}/device-groups/999")
        assert response.status_code == 404
        assert code(response) == "not_found"

    async def test_update_with_correct_version(self, client: AsyncClient) -> None:
        await login(client)
        created = (await client.post(f"{KNX}/device-groups", json={"name": "Foyer"})).json()
        response = await client.put(
            f"{KNX}/device-groups/{created['id']}",
            json={"name": "Renamed"},
            headers={"If-Unmodified-Since-Version": created["updated_at"]},
        )
        assert response.status_code == 200
        assert response.json()["name"] == "Renamed"

    async def test_references_for_an_unknown_group_is_not_found(self, client: AsyncClient) -> None:
        await login(client)
        response = await client.get(f"{KNX}/device-groups/999/references")
        assert response.status_code == 404
        assert code(response) == "not_found"


class TestUnsupportedAudit:
    """GET /knx/unsupported had no test at all before this audit."""

    async def test_shows_the_telegram_that_could_not_be_decoded(
        self, client: AsyncClient, services: Services, db: Database
    ) -> None:
        """§7.1 *Unsupported types*: a telegram whose DPT has no codec is
        recorded per address, keyed by group address, and named from the
        address library — mirroring
        ``tests/unit/core/test_knx.py::test_unsupported_dpt_is_logged_and_ignored``
        one layer up, through the HTTP endpoint rather than the subsystem
        directly. The address is created straight through
        :mod:`proskenion.db.crud.knx` because the create endpoint's own
        validation refuses a DPT outside the supported list (§7.1) — a
        library entry with an unsupported DPT can only arise from data that
        predates support for it, or an import, not a fresh manual add.
        """
        await login(client)
        await knx_crud.create_address(
            db,
            group_address="6/0/0",
            name="Fire Curtain Status",
            dpt="6.001",  # not in §7.1's supported table
            direction="incoming",
        )
        await services.registry.reload()

        raw = bytes([0x00, 0x80, 0x2A])
        await services.stub.emit("1.1.1", "6/0/0", raw)
        await asyncio.sleep(0.1)  # let the read loop record it

        response = await client.get(f"{KNX}/unsupported")
        assert response.status_code == 200
        body = response.json()
        assert len(body) == 1
        assert body[0]["group_address"] == "6/0/0"
        assert body[0]["name"] == "Fire Curtain Status"
        assert body[0]["dpt"] == "6.001"
        assert body[0]["raw"] == raw.hex()

    async def test_without_a_running_subsystem_is_device_unavailable(
        self, config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
    ) -> None:
        """:func:`proskenion.api.deps.get_knx`'s own docstring promises "503
        when it has not started" — exercised here with an application built
        without a KNX subsystem on ``app.state``, rather than this module's
        ``app`` fixture, which always injects a live one over the stub."""
        bare_app = create_app(config, db=db, tokens=tokens, limiter=limiter)
        async with make_client(bare_app) as bare_client:
            await login(bare_client)
            response = await bare_client.get(f"{KNX}/unsupported")
            assert response.status_code == 503
            assert code(response) == "device_unavailable"
            assert detail(response)["reason"] == "not_started"


class TestExportAudit:
    """GET /knx/export had no failure test before this audit."""

    async def test_operator_cannot_export(self, client: AsyncClient) -> None:
        """Exporting the whole library has no domain failure mode of its
        own — it always succeeds for an admin — so the tier refusal is the
        only failure this endpoint can produce."""
        await login(client, OPERATOR_PASSWORD)
        response = await client.get(f"{KNX}/export")
        assert response.status_code == 403
        assert code(response) == "permission_denied"
