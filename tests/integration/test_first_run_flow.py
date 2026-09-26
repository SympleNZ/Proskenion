"""The Phase 1 milestone at the API level (spec §22.4, §18 Phase 1).

    "The system boots, services start, an admin completes first-run setup and
     logs in, and device status is displayed."

The same path the browser journey in ``tests/e2e/first-run.spec.ts`` walks,
without a browser, so the milestone is provable in a suite that runs on every
pull request: the first-run gate refuses, §10.4's seven steps run, the commit
closes the wizard, the admin signs in with the password the wizard set, the
Q8 stub video matrix is configured and reaches ``connected``, ``/system/health``
carries it, and disabling it makes it ``unconfigured`` again.

Every refusal is asserted on the §16.1 envelope's **code**, not merely on the
status (§22.4).
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import FastAPI
from httpx import AsyncClient, Response

from proskenion.api.app import API_PREFIX

AUTH = f"{API_PREFIX}/auth"
DEVICES = f"{API_PREFIX}/devices"
DRIVERS = f"{API_PREFIX}/drivers"
SETUP = f"{API_PREFIX}/setup"
SYSTEM = f"{API_PREFIX}/system"

#: §10.4: at least twelve characters. Twelve exactly, so the boundary is tested.
ADMIN_PASSWORD = "Twelve-Chars"
OPERATOR_PASSWORD = "OperatorPass"

#: How long the stub is given to connect. It is a loopback transport with
#: nothing to wait for, so this is a guard against a hang, not a real delay.
CONNECT_TIMEOUT_S = 5.0


def error(response: Response) -> dict[str, Any]:
    body: dict[str, Any] = response.json()
    return dict(body["error"])


def code(response: Response) -> str:
    return str(error(response)["code"])


async def walk_the_wizard(client: AsyncClient, *, configure_device: bool = False) -> None:
    """§10.4's seven steps, in order, ending with the commit."""
    welcome = await client.post(
        f"{SETUP}/step/1", json={"locale": "en_NZ.UTF-8", "timezone": "Pacific/Auckland"}
    )
    assert welcome.status_code == 200, welcome.text

    # Step 2 sets the admin password and issues the admin session the rest of
    # the wizard runs under, exactly as POST /auth/login would (§10.4, §6.4).
    admin = await client.post(
        f"{SETUP}/step/2",
        json={"password": ADMIN_PASSWORD, "password_confirm": ADMIN_PASSWORD},
    )
    assert admin.status_code == 200, admin.text
    assert admin.json()["tier"] == "admin"

    network = await client.post(f"{SETUP}/step/3", json={"skipped": True})
    assert network.status_code == 200, network.text

    device_ids: list[int] = []
    if configure_device:
        device_ids.append((await create_stub_device(client))["id"])
    devices = await client.post(
        f"{SETUP}/step/4", json={"device_ids": device_ids, "skipped": not device_ids}
    )
    assert devices.status_code == 200, devices.text

    operator = await client.post(
        f"{SETUP}/step/5",
        json={"password": OPERATOR_PASSWORD, "password_confirm": OPERATOR_PASSWORD},
    )
    assert operator.status_code == 200, operator.text

    certificate = await client.post(f"{SETUP}/step/6", json={"option": "self_signed"})
    assert certificate.status_code == 200, certificate.text

    review = await client.post(f"{SETUP}/step/7", json={"reviewed": True})
    assert review.status_code == 200, review.text

    committed = await client.post(f"{SETUP}/complete")
    assert committed.status_code == 200, committed.text
    assert committed.json()["first_run"] is False


async def create_stub_device(client: AsyncClient, name: str = "Foyer matrix") -> dict[str, Any]:
    """Configure decision Q8's stub video matrix over the loopback transport."""
    response = await client.post(
        DEVICES,
        json={
            "category": "video_matrix",
            "driver_key": "stub",
            "name": name,
            "config": {
                "transport": {"type": "loopback"},
                "driver": {"input_count": 4, "output_count": 2},
            },
        },
    )
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    return created


async def wait_for_status(client: AsyncClient, device_id: int, status: str) -> dict[str, Any]:
    """Poll the device until it reports ``status``. §12.1: connection is asynchronous.

    The interface is served before any device has confirmed, so a device that
    has just been configured reports ``connecting`` first; the manager's task
    connects, probes and reports separately (§5.3).
    """
    deadline = asyncio.get_running_loop().time() + CONNECT_TIMEOUT_S
    last: dict[str, Any] = {}
    while asyncio.get_running_loop().time() < deadline:
        response = await client.get(f"{DEVICES}/{device_id}")
        assert response.status_code == 200, response.text
        last = response.json()
        if last["status"] is not None and last["status"]["status"] == status:
            return last
        await asyncio.sleep(0.02)
    raise AssertionError(f"device {device_id} never reported {status!r}: {last.get('status')}")


# -- the gate before commissioning ----------------------------------------------


async def test_first_run_refuses_every_other_route(client: AsyncClient) -> None:
    """Decision Q4: 403 ``permission_denied`` with ``first_run_incomplete``."""
    refused = await client.get(f"{SYSTEM}/health")
    assert refused.status_code == 403
    assert code(refused) == "permission_denied"
    assert error(refused)["detail"]["reason"] == "first_run_incomplete"
    # …while /health and the wizard itself stay reachable (§16.7, §16.4).
    assert (await client.get("/health")).status_code == 200
    state = await client.get(f"{SETUP}/state")
    assert state.status_code == 200
    assert state.json()["first_run"] is True
    assert state.json()["next_step"] == 1


async def test_steps_must_be_submitted_in_order(client: AsyncClient) -> None:
    """§10.4's resumability is an ordering, and the server enforces it."""
    out_of_order = await client.post(
        f"{SETUP}/step/3", json={"address": "192.168.20.10", "skipped": False}
    )
    assert out_of_order.status_code == 422
    assert code(out_of_order) == "validation_failed"


async def test_a_short_admin_password_is_refused_with_field_detail(client: AsyncClient) -> None:
    """§10.4: minimum twelve characters, and the refusal names the field (§16.1)."""
    welcome = await client.post(
        f"{SETUP}/step/1", json={"locale": "en_NZ.UTF-8", "timezone": "Pacific/Auckland"}
    )
    assert welcome.status_code == 200
    short = await client.post(
        f"{SETUP}/step/2", json={"password": "too-short", "password_confirm": "too-short"}
    )
    assert short.status_code == 422
    assert code(short) == "validation_failed"
    fields = [field["field"] for field in error(short)["detail"]["fields"]]
    assert "password" in fields


# -- the milestone ---------------------------------------------------------------


async def test_the_phase_1_milestone(client: AsyncClient, app: FastAPI) -> None:
    await walk_the_wizard(client)

    # -- the wizard is a one-way door (§10.4, §16.4) -----------------------
    reopened = await client.get(f"{SETUP}/state")
    assert reopened.status_code == 403
    assert code(reopened) == "permission_denied"
    assert error(reopened)["detail"]["reason"] == "setup_complete"

    # -- an admin logs in with the password the wizard set (§6.3) ----------
    client.cookies.clear()
    wrong = await client.post(f"{AUTH}/login", json={"password": "not-the-password"})
    assert wrong.status_code == 401
    assert code(wrong) == "unauthenticated"

    signed_in = await client.post(f"{AUTH}/login", json={"password": ADMIN_PASSWORD})
    assert signed_in.status_code == 200, signed_in.text
    assert signed_in.json()["tier"] == "admin"

    # The gate is inert now: the route it refused above answers (decision Q4).
    assert (await client.get(f"{SYSTEM}/health")).status_code == 200

    # -- a device is configured and reports its status (§5.5, §11.1) -------
    drivers = await client.get(f"{DRIVERS}?category=video_matrix")
    assert drivers.status_code == 200
    assert [driver["key"] for driver in drivers.json()["drivers"]] == ["lkv422", "stub"]

    device = await create_stub_device(client)
    assert device["state_key"] == "hdmi"  # §5.6's slot for a video matrix
    connected = await wait_for_status(client, device["id"], "connected")
    assert connected["status"]["kind"] is None

    # Capabilities are resolved after connect(), never declared (B56).
    capabilities = await client.get(f"{DEVICES}/{device['id']}/capabilities")
    assert capabilities.status_code == 200, capabilities.text
    assert capabilities.json()["as_connected"] is True
    assert capabilities.json()["capabilities"]["input_count"] == 4

    # Connect and probe are reported separately (§5.3).
    tested = await client.post(f"{DEVICES}/{device['id']}/test")
    assert tested.status_code == 200, tested.text
    report = tested.json()
    assert report["ok"] is True
    assert report["connect"]["ok"] is True and report["probe"]["ok"] is True

    # -- device status is displayed (§11.1, §21.24) ------------------------
    # The endpoint serves the poller's snapshot for up to one poll interval
    # (§21.24 refreshes the screen at the same rate), so a device configured a
    # moment ago is not in the snapshot taken at boot. Polling now is what the
    # timer does; nothing about the payload is special-cased for the test.
    await app.state.health.poll()
    health = await client.get(f"{SYSTEM}/health")
    assert health.status_code == 200, health.text
    listed = {row["key"]: row for row in health.json()["devices"]}
    assert listed["hdmi"]["name"] == "Foyer matrix"
    assert listed["hdmi"]["status"] == "connected"
    assert listed["hdmi"]["level"] == "green"

    # -- disabled, and it says so ------------------------------------------
    disabled = await client.put(
        f"{DEVICES}/{device['id']}",
        headers={"If-Unmodified-Since-Version": connected["updated_at"]},
        json={"enabled": False},
    )
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["enabled"] is False
    off = await wait_for_status(client, device["id"], "unconfigured")
    assert off["status"]["detail"] == "This device is turned off"

    await app.state.health.poll()
    after = await client.get(f"{SYSTEM}/health")
    assert {row["key"]: row["status"] for row in after.json()["devices"]}["hdmi"] == "unconfigured"


async def test_a_stale_version_is_a_conflict(client: AsyncClient) -> None:
    """§16.1's optimistic concurrency, on the endpoint the milestone uses."""
    await walk_the_wizard(client)
    device = await create_stub_device(client)
    await wait_for_status(client, device["id"], "connected")

    stale = await client.put(
        f"{DEVICES}/{device['id']}",
        headers={"If-Unmodified-Since-Version": "2020-01-01T00:00:00+13:00"},
        json={"name": "Renamed"},
    )
    assert stale.status_code == 409
    assert code(stale) == "conflict"
    assert error(stale)["detail"]["current"]["name"] == device["name"]


async def test_the_device_step_configures_a_device_inside_the_wizard(client: AsyncClient) -> None:
    """§10.4 step 4: the wizard's own session reaches the admin-tier devices API.

    The gate exempts ``/drivers`` and ``/devices`` and step 2 signs the wizard
    in, so the step configures a real device rather than a pretend one; the
    step then records the ids that existed when it ran (T11b).
    """
    await walk_the_wizard(client, configure_device=True)
    devices = await client.get(DEVICES)
    assert devices.status_code == 200, devices.text
    configured = devices.json()["devices"]
    assert [row["driver_key"] for row in configured] == ["stub"]
    await wait_for_status(client, configured[0]["id"], "connected")


async def test_devices_need_the_wizards_session_during_first_run(client: AsyncClient) -> None:
    """The exemption is not a hole: without the cookie it is 401, not the gate's 403."""
    anonymous = await client.get(DEVICES)
    assert anonymous.status_code == 401
    assert code(anonymous) == "unauthenticated"
