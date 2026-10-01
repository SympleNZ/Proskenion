"""``tools/provision/stage_lighting.py`` against a served appliance.

The script runs on the appliance against ``http://127.0.0.1:8000``, so here
the application is served the same way: under uvicorn on a loopback port,
over a database file (the script's ``--mint-admin-session`` reads the admin
account's ``token_version`` from it, and the JWT secret from the state
directory the configuration file names). Its lighting output is the real
``artnet`` driver aimed at the Art-Net stub, and its KNX subsystem talks to
the knxd stub.

The appliance starts as the real one stood on 28 September 2026: first run
done, the eDMX8's artnet device configured, the stage's KNX addresses
imported from the site's two files (switches as ``both``, statuses as
``incoming``), the three seeded fixture profiles and the one seeded bar
("Proscenium"), and nothing else.

The journey is the one the README gives: a dry run that changes nothing, a
real run, and a second run that has nothing to do. The second run proves
idempotence twice over — its output says so, and every configuration row's
``updated_at`` is unchanged — so a script that duplicated or rewrote
anything fails here.

The upgrade journey starts from the shape the rig was provisioned with on
28 September and enabled on the 29th (one "Stage all" binding on ``4/0/8``
driving an ordinary "Stage all" group, statuses comparing stored levels) and
ends in the 30 September shape: "Stage all" indicator-only, four row
bindings on ``4/0/8`` enabled as the old one was, and every status comparing
what the room sees.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi import FastAPI

from proskenion.api.app import API_PREFIX, create_app
from proskenion.config import (
    AppSection,
    Config,
    DatabaseSection,
    Environment,
    KnxSection,
    LoggingSection,
)
from proskenion.core.auth import JWT_SECRET_FILENAME, TokenService
from proskenion.core.platform import DevelopmentPlatform
from proskenion.core.ratelimit import CLEAR_LOCKOUTS_FILENAME, RateLimiter
from proskenion.db.connection import Database
from proskenion.db.migrations import migrate
from tests.integration.rig import eventually, ok, until
from tests.integration.test_first_run_flow import ADMIN_PASSWORD, wait_for_status, walk_the_wizard
from tests.stubs.artnet_stub import ArtNetStub
from tests.stubs.knxd_stub import KnxdStub
from tools.provision import stage_lighting
from tools.provision.stage_lighting import (
    ALL,
    ALL_BINDINGS,
    BANKS,
    EXIT_CONFLICT,
    EXIT_OK,
    FIXTURE_COUNT,
    ROWS,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "knx"
COMMANDS_CSV = FIXTURES / "stage_commands_both.csv"
FEEDBACK_CSV = FIXTURES / "stage_feedback_incoming.csv"

#: The wall panel's individual address (network_map.md, "KNX group addresses seen on the bus").
PANEL = "1.1.27"
#: The controller's assigned individual address (the same section's "To do").
CONTROLLER = "1.1.3"
PASSWORD_VAR = "PROSKENION_TEST_ADMIN_PASSWORD"


@dataclass
class Appliance:
    origin: str
    config_file: Path
    client: httpx.AsyncClient
    app: FastAPI
    knxd: KnxdStub
    artnet_device_id: int


def _config_toml(config: Config) -> str:
    def path(value: Path) -> str:
        return value.as_posix()

    return (
        "[database]\n"
        f'path = "{path(Path(config.database.path))}"\n\n'
        "[logging]\n"
        f'path = "{path(Path(config.logging.path))}"\n\n'
        "[app]\n"
        'environment = "development"\n'
        f'state_dir = "{path(Path(config.app.state_dir))}"\n'
        f'data_dir = "{path(Path(config.app.data_dir))}"\n\n'
        "[knx]\n"
        f'host = "{config.knx.host}"\n'
        f"port = {config.knx.port}\n"
    )


async def _import(client: httpx.AsyncClient, csv: Path, direction: str) -> None:
    content = await asyncio.to_thread(csv.read_bytes)
    preview = ok(
        await client.post(
            f"{API_PREFIX}/knx/import",
            data={"step": "preview"},
            files={"file": (csv.name, content, "text/csv")},
        )
    )
    ok(
        await client.post(
            f"{API_PREFIX}/knx/import",
            data={"step": "confirm", "token": preview["token"], "direction": direction},
        )
    )


@pytest.fixture
async def appliance(tmp_path: Path, knxd: KnxdStub, artnet: ArtNetStub) -> AsyncIterator[Appliance]:
    config = Config(
        database=DatabaseSection(path=tmp_path / "auditorium.db"),
        logging=LoggingSection(path=tmp_path / "logs"),
        app=AppSection(
            environment=Environment.DEVELOPMENT,
            state_dir=tmp_path / "appliance",
            data_dir=tmp_path / "data",
        ),
        knx=KnxSection(host="127.0.0.1", port=knxd.port, individual_address=CONTROLLER),
    )
    config_file = tmp_path / "config.toml"
    config_file.write_text(_config_toml(config), encoding="utf-8", newline="\n")

    db = Database()
    await db.open(config.database.path)
    await migrate(db)
    app = create_app(
        config,
        db=db,
        tokens=TokenService(config.app.state_dir / JWT_SECRET_FILENAME),
        limiter=RateLimiter(signal_path=config.app.state_dir / CLEAR_LOCKOUTS_FILENAME),
    )
    async with contextlib.AsyncExitStack() as stack:
        stack.push_async_callback(db.close)
        await stack.enter_async_context(app.router.lifespan_context(app))
        # As tests/integration/conftest.py: the wizard's certificate step
        # writes under the platform's data directory, kept inside tmp_path.
        app.state.platform = DevelopmentPlatform(
            appliance_dir=Path(config.app.state_dir), data_dir=tmp_path / "data"
        )
        server = uvicorn.Server(
            uvicorn.Config(
                app, host="127.0.0.1", port=0, lifespan="off", log_config=None, access_log=False
            )
        )
        serving = asyncio.create_task(server.serve(), name="provisioning-uvicorn")

        async def stop() -> None:
            server.should_exit = True
            with contextlib.suppress(Exception):
                await asyncio.wait_for(serving, 10.0)

        stack.push_async_callback(stop)
        await until(lambda: server.started or serving.done(), "uvicorn to start")
        if serving.done():
            serving.result()
        port = int(server.servers[0].sockets[0].getsockname()[1])
        origin = f"http://127.0.0.1:{port}"

        client = await stack.enter_async_context(httpx.AsyncClient(base_url=origin, timeout=15.0))
        await walk_the_wizard(client)  # leaves the client signed in as admin
        device = ok(
            await client.post(
                f"{API_PREFIX}/devices",
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
        await wait_for_status(client, device["id"], "connected")
        await _import(client, COMMANDS_CSV, "both")
        await _import(client, FEEDBACK_CSV, "incoming")
        yield Appliance(origin, config_file, client, app, knxd, device["id"])


async def _run(appliance: Appliance, *extra: str, auth: str = "mint") -> tuple[int, str]:
    out = io.StringIO()
    login = (
        ["--mint-admin-session", "--config", str(appliance.config_file)]
        if auth == "mint"
        else ["--password-env", PASSWORD_VAR]
    )
    code = await stage_lighting.run(
        ["--base-url", appliance.origin, *login, "--dmx-host", "127.0.0.1", *extra], out
    )
    return code, out.getvalue()


async def _configuration(client: httpx.AsyncClient) -> dict[str, Any]:
    """Every configuration row the script could touch, as the API reports it."""
    return {
        "profiles": ok(await client.get(f"{API_PREFIX}/lighting/profiles"))["profiles"],
        "bars": ok(await client.get(f"{API_PREFIX}/lighting/bars"))["bars"],
        "channels": ok(await client.get(f"{API_PREFIX}/lighting/channels"))["channels"],
        "groups": ok(await client.get(f"{API_PREFIX}/lighting/groups"))["groups"],
        "addresses": ok(await client.get(f"{API_PREFIX}/knx/addresses")),
        "rules": ok(await client.get(f"{API_PREFIX}/rules"))["rules"],
        "statuses": ok(await client.get(f"{API_PREFIX}/derived-status"))["derived_statuses"],
    }


def _without_live_fields(configuration: dict[str, Any]) -> dict[str, Any]:
    """The rows minus what changes without a write (a rule's next firing, the
    KNX library's per-address unsupported-telegram record)."""
    stable = dict(configuration)
    stable["rules"] = [{k: v for k, v in r.items() if k != "next_fire_at"} for r in stable["rules"]]
    stable["addresses"] = [
        {k: v for k, v in a.items() if k != "unsupported"} for a in stable["addresses"]
    ]
    return stable


def _show(text: str) -> None:
    """Print for ``-s``, whatever the console's encoding (the rule names carry "→")."""
    encoding = sys.stdout.encoding or "utf-8"
    print(text.encode(encoding, "backslashreplace").decode(encoding))


def _no_secrets(output: str) -> None:
    assert ADMIN_PASSWORD not in output
    assert "eyJ" not in output  # the start of every JWT's encoded header


async def test_provisions_the_stage_once_and_only_once(
    appliance: Appliance, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client = appliance.client
    before = await _configuration(client)
    assert [b["name"] for b in before["bars"]] == ["Proscenium"]
    assert before["channels"] == [] and before["groups"] == [] and before["rules"] == []

    # -- the dry run: the whole plan, and nothing changed -----------------------------
    code, dry = await _run(appliance, "--dry-run")
    with capsys.disabled():
        _show("\n----- dry-run output -----\n" + dry + "----- end -----")
    assert code == EXIT_OK, dry
    _no_secrets(dry)
    assert "Dry run: 36 to create, 5 to change. Nothing was changed." in dry
    assert "INDICATOR ONLY (no fader)" in dry
    assert "Profile         1 'Single-channel dimmer' (1 channel: dimmer)" in dry
    assert _without_live_fields(await _configuration(client)) == _without_live_fields(before)

    # -- the real run --------------------------------------------------------------------
    code, applied = await _run(appliance)
    assert code == EXIT_OK, applied
    _no_secrets(applied)
    assert "Done: 36 created, 5 changed. No patch conflicts." in applied

    after = await _configuration(client)
    bars = {b["name"]: b for b in after["bars"]}
    assert {name: bar["sort_order"] for name, bar in bars.items()} == {
        "Proscenium": 0,
        "Row 2": 1,
        "Row 3": 2,
        "Row 4 (back)": 3,
    }

    channels = {c["name"]: c for c in after["channels"]}
    assert len(after["channels"]) == FIXTURE_COUNT
    by_id = {c["id"]: c["name"] for c in after["channels"]}
    rows = {
        "Proscenium": (1, 2, 3, 4),
        "Row 2": (5, 6, 7, 8),
        "Row 3": (9, 10, 11, 12),
        "Row 4 (back)": (13, 14, 15),
    }
    for bar_name, numbers in rows.items():
        spacing = [round((i + 1) / (len(numbers) + 1), 3) for i in range(len(numbers))]
        for number, position in zip(numbers, spacing, strict=True):
            channel = channels[f"Stage {number}"]
            assert channel["type"] == "dmx"
            assert channel["device_id"] == appliance.artnet_device_id
            assert (channel["universe"], channel["address"]) == (0, number)
            assert channel["profile_id"] == 1
            assert channel["bar_id"] == bars[bar_name]["id"]
            assert channel["position"] == position
    assert ok(await client.get(f"{API_PREFIX}/lighting/patch/conflicts")) == {"conflicts": []}

    groups = {g["name"]: sorted(by_id[i] for i in g["channel_ids"]) for g in after["groups"]}
    assert groups == {bank.group: sorted(f"Stage {n}" for n in bank.fixtures) for bank in BANKS}
    assert {g["name"]: g["indicator_only"] for g in after["groups"]} == {
        bank.group: bank.indicator_only for bank in BANKS
    }
    assert {g["name"] for g in after["groups"] if g["indicator_only"]} == {"Stage all"}

    addresses = {a["group_address"]: a for a in after["addresses"]}
    for bank in BANKS:
        assert addresses[bank.switch]["direction"] == "both"
        assert addresses[bank.status]["direction"] == "both"
    assert addresses["0/0/2"]["direction"] == "incoming"  # not the script's

    _assert_new_shape(after, enabled=False)

    # -- disabled means inert: a panel press moves nothing and writes no status -----
    writes = len(appliance.knxd.writes)
    await appliance.knxd.send_telegram("4/0/8", "1.001", True, source_address=PANEL)
    # Nothing can be waited for: the assertion is that nothing happens. One
    # second is well past a binding's reaction on loopback (milliseconds).
    await asyncio.sleep(1.0)
    look = ok(await client.get(f"{API_PREFIX}/lighting/state"))
    assert all(entry.get("level", 0) == 0 for entry in look["channels"].values()), look
    statuses_written = [
        w for w in appliance.knxd.writes[writes:] if w.group_address.startswith("4/0/")
    ]
    assert statuses_written == []

    # -- the second run: nothing to do, and nothing touched ------------------------------
    code, again = await _run(appliance)
    assert code == EXIT_OK, again
    assert "Nothing to do: the stage lighting is already provisioned as specified." in again
    monkeypatch.setenv(PASSWORD_VAR, ADMIN_PASSWORD)
    code, by_password = await _run(appliance, auth="password")
    assert code == EXIT_OK, by_password
    _no_secrets(by_password)
    assert "Session: admin, signed in with $" in by_password
    assert "Nothing to do" in by_password
    assert _without_live_fields(await _configuration(client)) == _without_live_fields(after)


def _assert_new_shape(configuration: dict[str, Any], *, enabled: bool) -> None:
    """Rows bound to their own switches, the "all" switch bound to every row,
    no binding on "Stage all", and every status "all at 100 %" as the room
    sees it — each one ``enabled`` as asked."""
    addresses = {a["group_address"]: a for a in configuration["addresses"]}
    group_ids = {g["name"]: g["id"] for g in configuration["groups"]}
    wanted = [(row.group, row.switch, row.group) for row in ROWS] + [
        (name, ALL.switch, row.group) for name, row in ALL_BINDINGS
    ]
    rules = sorted(configuration["rules"], key=lambda r: r["name"])
    assert [r["name"] for r in rules] == sorted(name for name, _, _ in wanted)
    by_name = {r["name"]: r for r in rules}
    for name, switch, group in wanted:
        rule = by_name[name]
        assert rule["enabled"] is enabled, name
        assert rule["knx_address_id"] == addresses[switch]["id"]
        assert (rule["match_type"], rule["action_type"]) == ("any", "lighting_group")
        assert rule["lighting_group_id"] == group_ids[group]
        assert (rule["on_level"], rule["off_level"], rule["fade_ms"]) == (100.0, 0.0, 0)
        assert rule["debounce_ms"] == 500
    assert group_ids["Stage all"] not in {r["lighting_group_id"] for r in rules}
    assert len(configuration["statuses"]) == len(BANKS)
    for status, bank in zip(configuration["statuses"], BANKS, strict=True):
        assert status["name"] == f"{bank.group} indicator"
        assert status["enabled"] is enabled
        assert status["group_address"] == bank.status
        assert status["source_type"] == "lighting_group_all_at"
        assert (status["lighting_group_id"], status["compare_level"], status["basis"]) == (
            group_ids[bank.group],
            100.0,
            "output",
        )


async def _make_the_28_september_shape(client: httpx.AsyncClient) -> None:
    """Turn a freshly provisioned installation back into the shape the rig
    has run since the 29 September swap-over: "Stage all" an ordinary group
    driven by one enabled binding on ``4/0/8``, every binding and status
    enabled, the statuses comparing stored levels."""
    configuration = await _configuration(client)
    group_ids = {g["name"]: g["id"] for g in configuration["groups"]}
    addresses = {a["group_address"]: a for a in configuration["addresses"]}
    for rule in configuration["rules"]:
        if rule["name"] in {name for name, _ in ALL_BINDINGS}:
            ok(await client.delete(f"{API_PREFIX}/rules/{rule['id']}"), 204)
        else:
            ok(
                await client.put(
                    f"{API_PREFIX}/rules/{rule['id']}",
                    json={"enabled": True},
                    headers={"If-Unmodified-Since-Version": rule["updated_at"]},
                )
            )
    stage_all = next(g for g in configuration["groups"] if g["name"] == "Stage all")
    ok(
        await client.put(
            f"{API_PREFIX}/lighting/groups/{stage_all['id']}",
            json={"indicator_only": False},
            headers={"If-Unmodified-Since-Version": stage_all["updated_at"]},
        )
    )
    ok(
        await client.post(
            f"{API_PREFIX}/rules",
            json={
                "name": "Stage all",
                "enabled": True,
                "trigger_type": "knx",
                "knx_address_id": addresses[ALL.switch]["id"],
                "match_type": "any",
                "debounce_ms": 500,
                "action_type": "lighting_group",
                "lighting_group_id": group_ids["Stage all"],
                "on_level": 100.0,
                "off_level": 0.0,
                "fade_ms": 0,
            },
        ),
        201,
    )
    for status in configuration["statuses"]:
        ok(
            await client.put(
                f"{API_PREFIX}/derived-status/{status['id']}",
                json={"enabled": True, "basis": "level"},
                headers={"If-Unmodified-Since-Version": status["updated_at"]},
            )
        )


async def test_upgrades_the_rig_from_the_28_september_shape(
    appliance: Appliance, capsys: pytest.CaptureFixture[str]
) -> None:
    client = appliance.client
    code, _ = await _run(appliance)
    assert code == EXIT_OK
    await _make_the_28_september_shape(client)
    old = await _configuration(client)
    assert [g["indicator_only"] for g in old["groups"]] == [False] * len(BANKS)
    assert {r["name"] for r in old["rules"]} == {bank.group for bank in BANKS}
    assert all(r["enabled"] for r in old["rules"])
    assert {s["basis"] for s in old["statuses"]} == {"level"}
    # Nobody can make "Stage all" indicator-only while its binding drives it.
    stage_all = next(g for g in old["groups"] if g["name"] == "Stage all")
    refused = await client.put(
        f"{API_PREFIX}/lighting/groups/{stage_all['id']}",
        json={"indicator_only": True},
        headers={"If-Unmodified-Since-Version": stage_all["updated_at"]},
    )
    assert refused.status_code == 422, refused.text
    assert "indicator_only" in refused.json()["error"]["detail"]

    # -- the dry run against the rig's current shape --------------------------------
    code, dry = await _run(appliance, "--dry-run")
    with capsys.disabled():
        _show("\n----- upgrade dry-run output -----\n" + dry + "----- end -----")
    assert code == EXIT_OK, dry
    assert "Dry run: 4 to create, 6 to change, 1 to delete. Nothing was changed." in dry
    assert "delete binding  'Stage all'" in dry and "ENABLED" in dry
    for name, _ in ALL_BINDINGS:
        assert f"binding  {name!r} KNX 4/0/8" in dry
    assert "ENABLED (as the binding it replaces)" in dry
    assert "ordinary -> INDICATOR ONLY" in dry
    assert dry.count("(basis output)") == len(BANKS)
    assert _without_live_fields(await _configuration(client)) == _without_live_fields(old)

    # -- the real run -----------------------------------------------------------------
    code, applied = await _run(appliance)
    assert code == EXIT_OK, applied
    assert "Done: 4 created, 6 changed, 1 deleted. No patch conflicts." in applied
    after = await _configuration(client)
    _assert_new_shape(after, enabled=True)
    assert {g["name"] for g in after["groups"] if g["indicator_only"]} == {"Stage all"}

    # The "all" switch now switches the four rows; "Stage all indicator"
    # follows every fixture being at 100 % as the room sees it.
    writes = len(appliance.knxd.writes)
    await appliance.knxd.send_telegram("4/0/8", "1.001", True, source_address=PANEL)

    async def all_on() -> bool:
        look = ok(await client.get(f"{API_PREFIX}/lighting/state"))
        levels = [e.get("level") for e in look["channels"].values()]
        return len(levels) == FIXTURE_COUNT and all(level == 100.0 for level in levels)

    await eventually(all_on, "every stage fixture at 100 %")

    def last_written(address: str) -> object:
        values = [
            w.value("1.001") for w in appliance.knxd.writes[writes:] if w.group_address == address
        ]
        return values[-1] if values else None

    for bank in BANKS:
        await until(lambda bank=bank: last_written(bank.status) is True, f"{bank.status} on")

    # -- the second run: nothing to do --------------------------------------------------
    code, again = await _run(appliance)
    assert code == EXIT_OK, again
    assert "Nothing to do: the stage lighting is already provisioned as specified." in again
    assert _without_live_fields(await _configuration(client)) == _without_live_fields(after)


async def test_a_page_button_on_the_old_all_binding_is_a_conflict(appliance: Appliance) -> None:
    client = appliance.client
    code, _ = await _run(appliance)
    assert code == EXIT_OK
    await _make_the_28_september_shape(client)
    old_rule = next(
        r for r in (await _configuration(client))["rules"] if r["name"] == "Stage all"
    )
    page = ok(await client.post(f"{API_PREFIX}/pages", json={"name": "Stage"}), 201)
    ok(
        await client.put(
            f"{API_PREFIX}/pages/{page['id']}",
            json={
                "name": "Stage",
                "sort_order": page["sort_order"],
                "items": [
                    {
                        "kind": "panel",
                        "panel_title": "Stage",
                        "panel_width": 1,
                        "buttons": [
                            {"col": 0, "row": 0, "label": "All", "rule_id": old_rule["id"]}
                        ],
                    }
                ],
            },
            headers={"If-Unmodified-Since-Version": page["updated_at"]},
        )
    )
    before = await _configuration(client)
    code, output = await _run(appliance)
    assert code == EXIT_CONFLICT, output
    assert "page 'Stage' button 'All' fires rule 'Stage all'" in output
    assert _without_live_fields(await _configuration(client)) == _without_live_fields(before)


async def test_a_foreign_fixture_in_the_stage_range_stops_the_run_before_any_change(
    appliance: Appliance,
) -> None:
    client = appliance.client
    ok(
        await client.post(
            f"{API_PREFIX}/lighting/channels",
            json={
                "name": "Follow spot",
                "type": "dmx",
                "profile_id": 1,
                "device_id": appliance.artnet_device_id,
                "universe": 0,
                "address": 7,
            },
        ),
        201,
    )
    before = await _configuration(client)
    code, output = await _run(appliance)
    assert code == EXIT_CONFLICT, output
    assert "fixture 'Follow spot'" in output and "inside the stage's 1-15" in output
    assert "nothing has been changed" in output
    assert _without_live_fields(await _configuration(client)) == _without_live_fields(before)


async def test_an_operator_password_is_refused(
    appliance: Appliance, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.integration.test_first_run_flow import OPERATOR_PASSWORD

    monkeypatch.setenv(PASSWORD_VAR, OPERATOR_PASSWORD)
    code, output = await _run(appliance, "--dry-run", auth="password")
    assert code == stage_lighting.EXIT_CANNOT_START, output
    assert "signs in as operator; admin is needed" in output
    assert OPERATOR_PASSWORD not in output
