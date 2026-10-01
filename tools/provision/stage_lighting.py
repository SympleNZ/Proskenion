"""Provision the auditorium's stage lighting through Proskenion's own HTTP API.

What it configures (``docs/hardware/network_map.md``, "Stage lighting rows"):

* 15 plain white fixtures, "Stage 1" to "Stage 15", one DMX channel each, on
  universe 0, addresses 1 to 15, out through the eDMX8 MAX's ``artnet`` device;
* four lighting bars, one per row, front to back: row 1 is the existing
  "Proscenium" bar, then "Row 2", "Row 3" and "Row 4 (back)" are created
  behind it; each row's fixtures are spaced evenly across its bar;
* five lighting groups: "Stage row 1" to "Stage row 4", and "Stage all",
  which is **indicator-only** (owner decision 2026-09-30, "Stage all as a
  master"): it never scales output and has no fader, so the row faders always
  work; the Lighting view's Master is the whole-stage fader. It exists only so
  its derived status can light the panel's "all on" indicator;
* for each row, a §8.2 binding (shape B: the panel's switch address drives
  the group to 100 % or 0 %); for the panel's "all" switch (``4/0/8``), four
  bindings "Stage all → row 1" to "→ row 4", one per row group, because a
  binding cannot target an indicator-only group;
* for each of the five groups, a §8.6 derived status (shape C: the group's
  state written back on the panel's status address): on only while every
  fixture in the group is at 100 % **as the room sees it** — its output after
  the master (level × master; ``basis`` ``output``, migration 011), so a
  row fader or the master pulled down turns the lamp off.

On a fresh installation every binding and status is created **disabled** —
the legacy controller still answers the panel until the swap-over.

**Upgrading the earlier shape** (one binding "Stage all" on ``4/0/8`` driving
the "Stage all" group, which was an ordinary group): the plan deletes that
binding, creates the four row bindings on ``4/0/8`` — **enabled if the old
binding was enabled** (it is, on the rig since the 2026-09-29 swap-over), so
the panel's "all" button keeps working — and then makes "Stage all"
indicator-only. The order matters: the API refuses to make a group
indicator-only while a binding still drives it. Each existing status that
compares the stored level is changed to compare the output, enabled or
disabled as it is. A page button still firing
the old binding is a conflict (the API refuses to delete a rule a button
fires); nothing is changed until it is re-pointed by hand.

It is idempotent. Everything is looked up first — fixtures, bars, groups and
bindings by name, KNX addresses and derived statuses by group address, the
fixture profile by its shape, the DMX output by driver and host — and only
what is missing is created. Something that exists but is configured
differently is a **conflict**: the whole run stops before changing anything
and says what differs. The only thing it ever deletes is the earlier shape's
"Stage all" binding (above); it never renames anything. The one change it
may make to something it did not create is a status address's direction,
``incoming`` to ``both``: §8.9 has a derived status write its address, and the
API refuses a status on an incoming-only one (``proskenion/api/rules.py``).

``--dry-run`` prints the same plan and changes nothing.

Authentication is the application's own session cookie, by one of two routes:

``--mint-admin-session``
    For running on the appliance as the application's user: the JWT secret
    is read from the state directory named by the application's
    configuration, and the admin account's ``token_version`` from its
    database, and a session is signed exactly as ``POST /auth/login`` would
    sign one, but with a five-minute lifetime. Nothing re-issues it.
``--password-env VAR``
    A normal ``POST /auth/login`` with the admin password taken from the
    environment variable ``VAR``.

The token and the password are never printed.

Exit status: 0 done (or nothing to do), 1 an error part-way, 2 conflicts
(nothing was changed), 3 unable to start (authentication, configuration).
"""

from __future__ import annotations

import argparse
import asyncio
import io
import os
import sqlite3
import sys
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any, TextIO

import httpx

from proskenion.config import ConfigError, load_config
from proskenion.core.auth import COOKIE_NAME, JWT_SECRET_FILENAME, TokenService, iso

API_PREFIX = "/api/v1"
DEFAULT_BASE_URL = "http://127.0.0.1:8000"
#: The eDMX8 MAX (network_map.md, "eDMX8 MAX ports"): port A is universe 0.
DEFAULT_DMX_HOST = "10.2.30.245"
UNIVERSE = 0
FIXTURE_COUNT = 15

#: A minted session's whole life, idle and absolute alike. The run takes
#: seconds; nothing in it re-issues the token (only ``GET /auth/session``
#: does, and this script never calls it).
MINTED_SESSION = timedelta(minutes=5)

#: Written into the notes of what this script creates, so an admin reading
#: the Fixtures, Bars or Rules screen knows where a row came from.
NOTE = "Provisioned by tools/provision/stage_lighting.py"

#: §8.2's binding levels and the rule editor's defaults (RuleEditor.tsx):
#: on 100 %, off 0 %, no fade, §8.4's 500 ms debounce.
ON_LEVEL = 100.0
OFF_LEVEL = 0.0
FADE_MS = 0
DEBOUNCE_MS = 500
#: The panel's indicators follow what the room sees — every member's output,
#: level × the master, at 100 % (migration 011, owner
#: decision 2026-09-30) — not the stored levels.
STATUS_BASIS = "output"

#: The profile created only when no existing one has a single dimmer channel.
DIMMER_PROFILE: dict[str, Any] = {
    "name": "Single-channel dimmer",
    "channel_count": 1,
    "channels": [{"offset": 0, "role": "dimmer", "default": 0}],
}

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFLICT = 2
EXIT_CANNOT_START = 3


@dataclass(frozen=True, slots=True)
class Bank:
    """One panel button: its group, binding, status and fixtures."""

    group: str
    colour: str
    fixtures: tuple[int, ...]
    switch: str
    status: str
    bar: str | None  # the row's bar; None for "all"

    @property
    def indicator_only(self) -> bool:
        """"Stage all" never scales output; it exists for its status only."""
        return self.bar is None

    @property
    def binding(self) -> str:
        return self.group

    @property
    def indicator(self) -> str:
        return f"{self.group} indicator"


#: Rows front (1) to back (4), then "all". Colours are §21.3's group palette
#: (tokens.css ``--group-*``): identity only, never status.
BANKS: tuple[Bank, ...] = (
    Bank("Stage row 1", "#00A5FE", (1, 2, 3, 4), "4/0/0", "4/0/4", "Proscenium"),
    Bank("Stage row 2", "#93C896", (5, 6, 7, 8), "4/0/1", "4/0/5", "Row 2"),
    Bank("Stage row 3", "#E8C26C", (9, 10, 11, 12), "4/0/2", "4/0/6", "Row 3"),
    Bank("Stage row 4", "#A482C9", (13, 14, 15), "4/0/3", "4/0/7", "Row 4 (back)"),
    Bank("Stage all", "#B4C0CC", tuple(range(1, FIXTURE_COUNT + 1)), "4/0/8", "4/0/9", None),
)
ROWS: tuple[Bank, ...] = tuple(b for b in BANKS if b.bar is not None)
ALL: Bank = next(b for b in BANKS if b.bar is None)


def all_binding_name(row_number: int) -> str:
    """The panel's "all" switch drives each row group through its own binding."""
    return f"{ALL.group} → row {row_number}"


#: ``(binding name, row bank)`` for the four bindings on the "all" switch.
ALL_BINDINGS: tuple[tuple[str, Bank], ...] = tuple(
    (all_binding_name(number), row) for number, row in enumerate(ROWS, start=1)
)


def fixture_name(number: int) -> str:
    return f"Stage {number}"


def spaced(index: int, count: int) -> float:
    """Evenly across a bar, clear of both ends: 4 give 0.2 ... 0.8, 3 give 0.25 ... 0.75.

    0.0 is stage right, drawn on the left of the plan (web/src/stageplan/layout.ts).
    """
    return round((index + 1) / (count + 1), 3)


def row_of(number: int) -> Bank:
    return next(r for r in ROWS if number in r.fixtures)


# -- the API ----------------------------------------------------------------------------


class ApiFailure(Exception):
    """A request the API refused; the message carries its §16.1 error envelope."""


class Api:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        response = await self._client.request(method, API_PREFIX + path, **kwargs)
        if response.status_code >= 300:
            raise ApiFailure(
                f"{method} {API_PREFIX}{path}: {response.status_code} {_envelope(response)}"
            )
        return response.json() if response.content else None

    async def get(self, path: str) -> Any:
        return await self._call("GET", path)

    async def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        created: dict[str, Any] = await self._call("POST", path, json=body)
        return created

    async def put(self, path: str, body: dict[str, Any], version: str) -> dict[str, Any]:
        updated: dict[str, Any] = await self._call(
            "PUT", path, json=body, headers={"If-Unmodified-Since-Version": version}
        )
        return updated

    async def delete(self, path: str) -> None:
        await self._call("DELETE", path)


def _envelope(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:300]
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        return f"{error.get('code')}: {error.get('message')} {error.get('detail') or ''}".strip()
    return str(body)[:300]


# -- authentication ------------------------------------------------------------------------


class CannotStart(Exception):
    """Configuration or authentication failed before anything was looked at."""


def mint_admin_token(config_path: str | None) -> tuple[str, str]:
    """An admin session token signed with the installation's own secret, and its expiry.

    The secret is read, never created: a missing file means this is not the
    application's state directory, and a new secret would sign a token the
    running application rejects.
    """
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        raise CannotStart(str(exc)) from exc
    secret = Path(config.app.state_dir) / JWT_SECRET_FILENAME
    if not secret.is_file():
        raise CannotStart(f"{secret}: no JWT secret there (is the state directory right?)")
    if not os.access(secret, os.R_OK):
        raise CannotStart(f"{secret}: not readable; run as the application's user")
    database = Path(config.database.path)
    if not database.is_file():
        raise CannotStart(f"{database}: no database there")
    try:
        conn = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT token_version FROM users WHERE tier = 'admin'").fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise CannotStart(f"{database}: cannot read the admin account: {exc}") from exc
    if row is None:
        raise CannotStart(f"{database}: there is no admin account")
    tokens = TokenService(secret, idle=MINTED_SESSION, absolute=MINTED_SESSION)
    token, claims = tokens.issue("admin", int(row[0]))
    return token, iso(claims.expires_at)


async def login_with_password(base_url: str, variable: str) -> tuple[str, str]:
    """``POST /auth/login`` with the password in ``$variable``; the session cookie's value.

    The cookie is taken from ``Set-Cookie`` directly: in production it is
    ``Secure``, and a cookie jar will not send a Secure cookie back over the
    plain HTTP of the loopback port.
    """
    password = os.environ.get(variable)
    if not password:
        raise CannotStart(f"${variable} is not set")
    async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as client:
        response = await client.post(f"{API_PREFIX}/auth/login", json={"password": password})
    if response.status_code != 200:
        raise CannotStart(f"sign-in refused: {response.status_code} {_envelope(response)}")
    body = response.json()
    if body.get("tier") != "admin":
        raise CannotStart(f"that password signs in as {body.get('tier')}; admin is needed")
    for header in response.headers.get_list("set-cookie"):
        cookie: SimpleCookie = SimpleCookie()
        cookie.load(header)
        if COOKIE_NAME in cookie:
            return cookie[COOKIE_NAME].value, str(body.get("expires_at"))
    raise CannotStart("sign-in succeeded but set no session cookie")


# -- the plan ------------------------------------------------------------------------------


@dataclass
class Step:
    verb: str  # "create", "change" or "delete"
    text: str
    run: Callable[[], Awaitable[None]]


@dataclass
class Plan:
    found: list[str] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class Provisioner:
    """Reads the installation, plans what is missing, and applies it."""

    def __init__(self, api: Api, *, dmx_device_id: int | None, dmx_host: str) -> None:
        self.api = api
        self.dmx_device_id = dmx_device_id
        self.dmx_host = dmx_host
        #: Ids by (kind, key), known now or filled in as steps create them.
        self.ids: dict[tuple[str, str], int] = {}
        #: The "Stage all" group as found, when it is still an ordinary group
        #: (the earlier shape): made indicator-only after the bindings.
        self._all_group_to_change: dict[str, Any] | None = None

    def id_of(self, kind: str, key: str) -> int:
        return self.ids[(kind, key)]

    async def plan(self) -> Plan:
        plan = Plan()
        api = self.api
        profiles = (await api.get("/lighting/profiles"))["profiles"]
        devices = (await api.get("/devices"))["devices"]
        bars = (await api.get("/lighting/bars"))["bars"]
        channels = (await api.get("/lighting/channels"))["channels"]
        groups = (await api.get("/lighting/groups"))["groups"]
        addresses = await api.get("/knx/addresses")
        rules = (await api.get("/rules"))["rules"]
        statuses = (await api.get("/derived-status"))["derived_statuses"]

        device = self._device(devices, plan)
        dimmer_profiles = self._profile(profiles, plan)
        self._bars(bars, channels, plan)
        if device is not None:
            self._channels(channels, profiles, dimmer_profiles, device, plan)
        self._groups(groups, plan)
        by_address = {a["group_address"]: a for a in addresses}
        self._addresses(by_address, plan)
        self._bindings(rules, by_address, plan)
        await self._all_bindings(rules, by_address, plan)
        self._all_indicator_only(plan)
        self._statuses(statuses, by_address, plan)
        return plan

    # -- the DMX output and the profile --

    def _device(self, devices: list[dict[str, Any]], plan: Plan) -> dict[str, Any] | None:
        artnet = [d for d in devices if d["driver_key"] == "artnet"]
        if self.dmx_device_id is not None:
            chosen = [d for d in artnet if d["id"] == self.dmx_device_id]
            wanted = f"artnet device {self.dmx_device_id}"
        else:
            chosen = [d for d in artnet if _host_of(d) == self.dmx_host]
            wanted = f"an artnet device at {self.dmx_host}"
        if len(chosen) != 1:
            listing = (
                ", ".join(f"{d['id']} {d['name']!r} ({_host_of(d)})" for d in artnet) or "none"
            )
            plan.conflicts.append(
                f"DMX output: expected exactly one {wanted}; artnet devices: {listing}"
            )
            return None
        device = chosen[0]
        self.ids[("device", "dmx")] = device["id"]
        plan.found.append(
            f"DMX output      device {device['id']} {device['name']!r} "
            f"(artnet, {_host_of(device)}), universe {UNIVERSE}"
        )
        return device

    def _profile(self, profiles: list[dict[str, Any]], plan: Plan) -> set[int]:
        """Every profile shaped as a single dimmer; the lowest id is the one used."""
        matching = sorted(p["id"] for p in profiles if _is_single_dimmer(p))
        if matching:
            chosen = next(p for p in profiles if p["id"] == matching[0])
            self.ids[("profile", "dimmer")] = chosen["id"]
            plan.found.append(
                f"Profile         {chosen['id']} {chosen['name']!r} (1 channel: dimmer)"
            )
            return set(matching)

        async def create() -> None:
            created = await self.api.post("/lighting/profiles", DIMMER_PROFILE)
            self.ids[("profile", "dimmer")] = created["id"]

        plan.steps.append(
            Step("create", f"profile  {DIMMER_PROFILE['name']!r} (1 channel: dimmer)", create)
        )
        return set()

    # -- bars --

    def _bars(self, bars: list[dict[str, Any]], channels: list[dict[str, Any]], plan: Plan) -> None:
        ours = {r.bar for r in ROWS}
        by_name: dict[str, list[dict[str, Any]]] = {}
        for bar in bars:
            by_name.setdefault(bar["name"], []).append(bar)
        foreign = [b for b in bars if b["name"] not in ours]
        base = max((b["sort_order"] for b in foreign), default=-1) + 1
        for bar in foreign:
            count = sum(1 for c in channels if c["bar_id"] == bar["id"])
            plan.notes.append(
                f"bar {bar['name']!r} (id {bar['id']}, sort_order {bar['sort_order']}, "
                f"{count} fixture{'s' if count != 1 else ''}) is not this script's; left as it is"
            )
        for index, row in enumerate(ROWS):
            assert row.bar is not None
            name = row.bar
            existing = by_name.get(name, [])
            if len(existing) > 1:
                plan.conflicts.append(f"bar {name!r}: {len(existing)} bars have this name")
                continue
            if existing:
                self.ids[("bar", name)] = existing[0]["id"]
                continue
            sort_order = base + index

            async def create(name: str = name, sort_order: int = sort_order) -> None:
                created = await self.api.post(
                    "/lighting/bars", {"name": name, "sort_order": sort_order, "notes": NOTE}
                )
                self.ids[("bar", name)] = created["id"]

            plan.steps.append(Step("create", f"bar      {name!r} sort_order {sort_order}", create))

    # -- fixtures --

    def _channels(
        self,
        channels: list[dict[str, Any]],
        profiles: list[dict[str, Any]],
        dimmer_profiles: set[int],
        device: dict[str, Any],
        plan: Plan,
    ) -> None:
        names = {fixture_name(n) for n in range(1, FIXTURE_COUNT + 1)}
        width = {p["id"]: int(p["channel_count"]) for p in profiles}
        for other in channels:
            if other["name"] in names or other["type"] != "dmx":
                continue
            if other["device_id"] != device["id"] or other["universe"] != UNIVERSE:
                continue
            low = int(other["address"])
            high = low + width.get(other["profile_id"], 1) - 1
            if low <= FIXTURE_COUNT and high >= 1:
                plan.conflicts.append(
                    f"fixture {other['name']!r} (id {other['id']}) is patched at "
                    f"{UNIVERSE}/{low}-{high}, inside the stage's 1-{FIXTURE_COUNT}"
                )
        for number in range(1, FIXTURE_COUNT + 1):
            name = fixture_name(number)
            row = row_of(number)
            assert row.bar is not None
            index = row.fixtures.index(number)
            position = spaced(index, len(row.fixtures))
            existing = [c for c in channels if c["name"] == name]
            if len(existing) > 1:
                plan.conflicts.append(f"fixture {name!r}: {len(existing)} fixtures have this name")
                continue
            if existing:
                channel = existing[0]
                wrong = _patch_differences(channel, device["id"], number, dimmer_profiles)
                if wrong:
                    plan.conflicts.append(
                        f"fixture {name!r} (id {channel['id']}) exists but {'; '.join(wrong)}"
                    )
                else:
                    self.ids[("channel", name)] = channel["id"]
                continue

            async def create(
                name: str = name,
                number: int = number,
                bar: str = row.bar,
                position: float = position,
            ) -> None:
                created = await self.api.post(
                    "/lighting/channels",
                    {
                        "name": name,
                        "type": "dmx",
                        "profile_id": self.id_of("profile", "dimmer"),
                        "device_id": self.id_of("device", "dmx"),
                        "universe": UNIVERSE,
                        "address": number,
                        "bar_id": self.id_of("bar", bar),
                        "position": position,
                        "notes": NOTE,
                    },
                )
                if created.get("conflicts"):
                    raise ApiFailure(
                        f"{name}: created, but the API reports a patch conflict: "
                        f"{created['conflicts']}"
                    )
                self.ids[("channel", name)] = created["id"]

            plan.steps.append(
                Step(
                    "create",
                    f"fixture  {name!r} DMX {UNIVERSE}/{number} on {row.bar!r} at {position}",
                    create,
                )
            )

    # -- groups --

    def _groups(self, groups: list[dict[str, Any]], plan: Plan) -> None:
        for sort_order, bank in enumerate(BANKS):
            existing = [g for g in groups if g["name"] == bank.group]
            if len(existing) > 1:
                plan.conflicts.append(
                    f"group {bank.group!r}: {len(existing)} groups have this name"
                )
                continue
            members = _span(bank.fixtures)
            if existing:
                group = existing[0]
                wanted = {self.ids.get(("channel", fixture_name(n))) for n in bank.fixtures}
                indicator_only = bool(group.get("indicator_only", False))
                if None in wanted or set(group["channel_ids"]) != wanted:
                    plan.conflicts.append(
                        f"group {bank.group!r} (id {group['id']}) exists but its members are not "
                        f"{members} (it has channel ids {group['channel_ids']})"
                    )
                elif indicator_only and not bank.indicator_only:
                    plan.conflicts.append(
                        f"group {bank.group!r} (id {group['id']}) is indicator-only; a row "
                        "group needs its fader"
                    )
                else:
                    self.ids[("group", bank.group)] = group["id"]
                    if bank.indicator_only and not indicator_only:
                        self._all_group_to_change = group
                continue

            async def create(bank: Bank = bank, sort_order: int = sort_order) -> None:
                created = await self.api.post(
                    "/lighting/groups",
                    {
                        "name": bank.group,
                        "channel_ids": [
                            self.id_of("channel", fixture_name(n)) for n in bank.fixtures
                        ],
                        "colour": bank.colour,
                        "sort_order": sort_order,
                        "indicator_only": bank.indicator_only,
                    },
                )
                self.ids[("group", bank.group)] = created["id"]

            kind = ", INDICATOR ONLY (no fader)" if bank.indicator_only else ""
            plan.steps.append(
                Step("create", f"group    {bank.group!r} = {members}{kind}", create)
            )

    # -- KNX addresses --

    def _addresses(self, by_address: dict[str, dict[str, Any]], plan: Plan) -> None:
        for bank in BANKS:
            for group_address, role in ((bank.switch, "switch"), (bank.status, "status")):
                address = by_address.get(group_address)
                if address is None:
                    plan.conflicts.append(
                        f"KNX {group_address} ({bank.group} {role}) is not in the KNX library; "
                        "import it first"
                    )
                    continue
                self.ids[("address", group_address)] = address["id"]
                if not str(address["dpt"]).startswith("1."):
                    plan.conflicts.append(
                        f"KNX {group_address} is DPT {address['dpt']}; a {role} address is 1-bit"
                    )
                    continue
                direction = address["direction"]
                if role == "switch" and direction == "outgoing":
                    plan.conflicts.append(
                        f"KNX {group_address} is outgoing-only; the panel's switch must be received"
                    )
                if role == "status" and direction == "incoming":

                    async def change(address: dict[str, Any] = address) -> None:
                        await self.api.put(
                            f"/knx/addresses/{address['id']}",
                            {"direction": "both"},
                            address["updated_at"],
                        )

                    plan.steps.append(
                        Step(
                            "change",
                            f"KNX      {group_address} {address['name']!r} direction incoming -> "
                            "both (so its status can write it once enabled)",
                            change,
                        )
                    )
        found = [
            f"{b.switch} + {b.status}"
            for b in BANKS
            if ("address", b.switch) in self.ids and ("address", b.status) in self.ids
        ]
        if found:
            plan.found.append(f"KNX addresses   {', '.join(found)} (switch + status)")

    # -- bindings (§8.2 shape B) --

    def _bindings(
        self, rules: list[dict[str, Any]], by_address: dict[str, dict[str, Any]], plan: Plan
    ) -> None:
        """One binding per row, named as its group, on the row's switch."""
        for bank in ROWS:
            switch = by_address.get(bank.switch)
            if switch is None:
                continue
            named = [r for r in rules if r["name"] == bank.binding]
            on_switch = [
                r
                for r in rules
                if r["trigger_type"] == "knx"
                and r["knx_address_id"] == switch["id"]
                and r["name"] != bank.binding
            ]
            for other in on_switch:
                plan.conflicts.append(
                    f"rule {other['name']!r} (id {other['id']}, "
                    f"{'enabled' if other['enabled'] else 'disabled'}) already triggers on "
                    f"{bank.switch}"
                )
            if len(named) > 1:
                plan.conflicts.append(f"rule {bank.binding!r}: {len(named)} rules have this name")
                continue
            if named:
                rule = named[0]
                group_id = self.ids.get(("group", bank.group))
                wrong = _binding_differences(rule, switch["id"], group_id)
                if wrong:
                    plan.conflicts.append(
                        f"rule {bank.binding!r} (id {rule['id']}) exists but {'; '.join(wrong)}"
                    )
                elif rule["enabled"]:
                    plan.notes.append(
                        f"binding {bank.binding!r} (id {rule['id']}) exists and is ENABLED; "
                        "left as it is"
                    )
                continue

            async def create(bank: Bank = bank) -> None:
                created = await self.api.post(
                    "/rules",
                    {
                        "name": bank.binding,
                        "enabled": False,
                        "notes": NOTE,
                        "trigger_type": "knx",
                        "knx_address_id": self.id_of("address", bank.switch),
                        "match_type": "any",
                        "debounce_ms": DEBOUNCE_MS,
                        "action_type": "lighting_group",
                        "lighting_group_id": self.id_of("group", bank.group),
                        "on_level": ON_LEVEL,
                        "off_level": OFF_LEVEL,
                        "fade_ms": FADE_MS,
                    },
                )
                if created.get("enabled") is not False:
                    raise ApiFailure(
                        f"binding {bank.binding!r} (id {created.get('id')}) was stored ENABLED; "
                        "disable it in Admin -> Rules now"
                    )

            plan.steps.append(
                Step(
                    "create",
                    f"binding  {bank.binding!r} KNX {bank.switch} -> group {bank.group!r} "
                    f"{ON_LEVEL:g} % / {OFF_LEVEL:g} %, fade {FADE_MS} ms, DISABLED",
                    create,
                )
            )

    async def _all_bindings(
        self, rules: list[dict[str, Any]], by_address: dict[str, dict[str, Any]], plan: Plan
    ) -> None:
        """The panel's "all" switch: four bindings, one per row group.

        "Stage all" is indicator-only, and a binding cannot target it (the API
        refuses one). The earlier shape's single "Stage all" binding is
        deleted first; the four row bindings are enabled exactly when it was
        (or, part-way through an earlier upgrade, when any of the four
        already is), so the panel's "all" button keeps working across the
        upgrade.
        """
        switch = by_address.get(ALL.switch)
        if switch is None:
            return
        wanted_names = {name for name, _ in ALL_BINDINGS}
        old = [r for r in rules if r["name"] == ALL.binding]
        for other in rules:
            if (
                other["trigger_type"] == "knx"
                and other["knx_address_id"] == switch["id"]
                and other["name"] not in wanted_names | {ALL.binding}
            ):
                plan.conflicts.append(
                    f"rule {other['name']!r} (id {other['id']}, "
                    f"{'enabled' if other['enabled'] else 'disabled'}) already triggers on "
                    f"{ALL.switch}"
                )
        if len(old) > 1:
            plan.conflicts.append(f"rule {ALL.binding!r}: {len(old)} rules have this name")
            return
        enabled = False
        if old:
            rule = old[0]
            wrong = _binding_differences(rule, switch["id"], self.ids.get(("group", ALL.group)))
            if wrong:
                plan.conflicts.append(
                    f"rule {ALL.binding!r} (id {rule['id']}) exists but {'; '.join(wrong)}"
                )
                return
            fired_by = await self._buttons_firing(rule["id"])
            for page, label in fired_by:
                plan.conflicts.append(
                    f"page {page!r} button {label!r} fires rule {ALL.binding!r} "
                    f"(id {rule['id']}), which is to be deleted; point the button at another "
                    "rule or remove it first"
                )
            if fired_by:
                return
            enabled = bool(rule["enabled"])

            async def delete(rule: dict[str, Any] = rule) -> None:
                await self.api.delete(f"/rules/{rule['id']}")

            plan.steps.append(
                Step(
                    "delete",
                    f"binding  {ALL.binding!r} (id {rule['id']}, "
                    f"{'ENABLED' if enabled else 'disabled'}) KNX {ALL.switch} -> group "
                    f"{ALL.group!r}: replaced by the four row bindings below",
                    delete,
                )
            )
        existing = {name: [r for r in rules if r["name"] == name] for name in wanted_names}
        if not old:
            enabled = any(r["enabled"] for found in existing.values() for r in found)
        for name, row in ALL_BINDINGS:
            named = existing[name]
            if len(named) > 1:
                plan.conflicts.append(f"rule {name!r}: {len(named)} rules have this name")
                continue
            if named:
                rule = named[0]
                wrong = _binding_differences(
                    rule, switch["id"], self.ids.get(("group", row.group))
                )
                if wrong:
                    plan.conflicts.append(
                        f"rule {name!r} (id {rule['id']}) exists but {'; '.join(wrong)}"
                    )
                elif rule["enabled"]:
                    plan.notes.append(
                        f"binding {name!r} (id {rule['id']}) exists and is ENABLED; left as it is"
                    )
                continue

            async def create(name: str = name, row: Bank = row, enabled: bool = enabled) -> None:
                created = await self.api.post(
                    "/rules",
                    {
                        "name": name,
                        "enabled": enabled,
                        "notes": NOTE,
                        "trigger_type": "knx",
                        "knx_address_id": self.id_of("address", ALL.switch),
                        "match_type": "any",
                        "debounce_ms": DEBOUNCE_MS,
                        "action_type": "lighting_group",
                        "lighting_group_id": self.id_of("group", row.group),
                        "on_level": ON_LEVEL,
                        "off_level": OFF_LEVEL,
                        "fade_ms": FADE_MS,
                    },
                )
                if created.get("enabled") is not enabled:
                    raise ApiFailure(
                        f"binding {name!r} (id {created.get('id')}) was stored "
                        f"{'enabled' if created.get('enabled') else 'disabled'}, not "
                        f"{'enabled' if enabled else 'disabled'}; fix it in Admin -> Rules now"
                    )

            plan.steps.append(
                Step(
                    "create",
                    f"binding  {name!r} KNX {ALL.switch} -> group {row.group!r} "
                    f"{ON_LEVEL:g} % / {OFF_LEVEL:g} %, fade {FADE_MS} ms, "
                    f"{'ENABLED (as the binding it replaces)' if enabled else 'DISABLED'}",
                    create,
                )
            )

    async def _buttons_firing(self, rule_id: int) -> list[tuple[str, str]]:
        """``(page name, button label)`` for every page button that fires ``rule_id``."""
        found: list[tuple[str, str]] = []
        for summary in (await self.api.get("/pages"))["pages"]:
            page = await self.api.get(f"/pages/{summary['id']}")
            for item in page.get("items", []):
                for button in item.get("buttons") or []:
                    if button.get("rule_id") == rule_id:
                        found.append((str(page["name"]), str(button["label"])))
        return found

    def _all_indicator_only(self, plan: Plan) -> None:
        """Make the existing "Stage all" group indicator-only — after its old
        binding is gone, since the API refuses it while a binding drives it."""
        group = self._all_group_to_change
        if group is None:
            return

        async def change(group: dict[str, Any] = group) -> None:
            await self.api.put(
                f"/lighting/groups/{group['id']}", {"indicator_only": True}, group["updated_at"]
            )

        plan.steps.append(
            Step(
                "change",
                f"group    {ALL.group!r} (id {group['id']}) ordinary -> INDICATOR ONLY "
                "(no fader; the row faders always work, the Master is the whole-stage fader)",
                change,
            )
        )

    # -- derived statuses (§8.2 shape C) --

    def _statuses(
        self, statuses: list[dict[str, Any]], by_address: dict[str, dict[str, Any]], plan: Plan
    ) -> None:
        for bank in BANKS:
            address = by_address.get(bank.status)
            if address is None:
                continue
            on_address = [s for s in statuses if s["knx_address_id"] == address["id"]]
            elsewhere = [
                s
                for s in statuses
                if s["name"] == bank.indicator and s["knx_address_id"] != address["id"]
            ]
            for other in elsewhere:
                plan.conflicts.append(
                    f"derived status {other['name']!r} (id {other['id']}) writes "
                    f"{other['group_address']}, not {bank.status}"
                )
            if on_address:
                status = on_address[0]
                group_id = self.ids.get(("group", bank.group))
                if (
                    status["source_type"] != "lighting_group_all_at"
                    or group_id is None
                    or status["lighting_group_id"] != group_id
                    or status["compare_level"] != ON_LEVEL
                ):
                    plan.conflicts.append(
                        f"KNX {bank.status} is already written by derived status "
                        f"{status['name']!r} (id {status['id']}), which is not "
                        f"{bank.group!r} all at {ON_LEVEL:g} %"
                    )
                else:
                    if status.get("basis", "level") != STATUS_BASIS:

                        async def change(status: dict[str, Any] = status) -> None:
                            await self.api.put(
                                f"/derived-status/{status['id']}",
                                {"basis": STATUS_BASIS},
                                status["updated_at"],
                            )

                        plan.steps.append(
                            Step(
                                "change",
                                f"status   {status['name']!r} (id {status['id']}) compares the "
                                f"stored level -> what the room sees (basis {STATUS_BASIS}); "
                                f"{'ENABLED' if status['enabled'] else 'disabled'} as it is",
                                change,
                            )
                        )
                    if status["enabled"]:
                        plan.notes.append(
                            f"status {status['name']!r} (id {status['id']}) exists and is "
                            "ENABLED; left enabled"
                        )
                continue

            async def create(bank: Bank = bank) -> None:
                created = await self.api.post(
                    "/derived-status",
                    {
                        "name": bank.indicator,
                        "enabled": False,
                        "knx_address_id": self.id_of("address", bank.status),
                        "source_type": "lighting_group_all_at",
                        "lighting_group_id": self.id_of("group", bank.group),
                        "compare_level": ON_LEVEL,
                        "basis": STATUS_BASIS,
                    },
                )
                if created.get("enabled") is not False:
                    raise ApiFailure(
                        f"status {bank.indicator!r} (id {created.get('id')}) was stored ENABLED; "
                        "disable it in Admin -> Rules -> Derived status now"
                    )

            plan.steps.append(
                Step(
                    "create",
                    f"status   {bank.indicator!r} KNX {bank.status} = group {bank.group!r} "
                    f"all at {ON_LEVEL:g} % as the room sees it, DISABLED",
                    create,
                )
            )

    # -- after --

    async def patch_conflicts(self) -> list[dict[str, Any]]:
        ours = {v for (kind, _), v in self.ids.items() if kind == "channel"}
        conflicts: list[dict[str, Any]] = (await self.api.get("/lighting/patch/conflicts"))[
            "conflicts"
        ]
        return [c for c in conflicts if ours & set(c["channel_ids"])]


def _host_of(device: dict[str, Any]) -> str | None:
    transport = (device.get("config") or {}).get("transport") or {}
    host = transport.get("host")
    return str(host) if host is not None else None


def _is_single_dimmer(profile: dict[str, Any]) -> bool:
    roles = [c["role"] for c in profile["channels"]]
    return int(profile["channel_count"]) == 1 and roles == ["dimmer"]


def _patch_differences(
    channel: dict[str, Any], device_id: int, address: int, profiles: set[int]
) -> list[str]:
    wrong: list[str] = []
    if channel["type"] != "dmx":
        wrong.append(f"is {channel['type']}, not dmx")
    if channel["device_id"] != device_id:
        wrong.append(f"is on device {channel['device_id']}, not {device_id}")
    if channel["universe"] != UNIVERSE or channel["address"] != address:
        wrong.append(
            f"is patched at {channel['universe']}/{channel['address']}, not {UNIVERSE}/{address}"
        )
    if channel["profile_id"] not in profiles:
        wrong.append(f"has profile {channel['profile_id']}, not a single dimmer")
    return wrong


def _binding_differences(rule: dict[str, Any], switch_id: int, group_id: int | None) -> list[str]:
    wrong: list[str] = []
    if rule["trigger_type"] != "knx" or rule["knx_address_id"] != switch_id:
        wrong.append("triggers on something else")
    if rule["match_type"] != "any":
        wrong.append(f"matches {rule['match_type']}, not any")
    if rule["action_type"] != "lighting_group" or group_id is None:
        wrong.append("does not drive this script's group")
    elif rule["lighting_group_id"] != group_id:
        wrong.append(f"drives group {rule['lighting_group_id']}, not {group_id}")
    if rule["on_level"] != ON_LEVEL or rule["off_level"] != OFF_LEVEL:
        wrong.append(f"levels are {rule['on_level']} / {rule['off_level']}")
    return wrong


def _span(numbers: tuple[int, ...]) -> str:
    names = [fixture_name(n) for n in numbers]
    return names[0] if len(names) == 1 else f"{names[0]} .. {names[-1]} ({len(names)} fixtures)"


# -- the command ---------------------------------------------------------------------------


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m tools.provision.stage_lighting",
        description="Provision the stage lighting through Proskenion's HTTP API (idempotent).",
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help=f"default {DEFAULT_BASE_URL}")
    auth = parser.add_mutually_exclusive_group(required=True)
    auth.add_argument(
        "--mint-admin-session",
        action="store_true",
        help="sign a short admin session with the installation's own secret (on the appliance)",
    )
    auth.add_argument(
        "--password-env",
        metavar="VAR",
        help="sign in with the admin password held in environment variable VAR",
    )
    parser.add_argument(
        "--config",
        help="the application's config.toml, for --mint-admin-session "
        "(default: $PROSKENION_CONFIG, then /opt/auditorium/config.toml)",
    )
    parser.add_argument("--dry-run", action="store_true", help="print the plan; change nothing")
    parser.add_argument("--dmx-host", default=DEFAULT_DMX_HOST, help=f"default {DEFAULT_DMX_HOST}")
    parser.add_argument(
        "--dmx-device-id", type=int, help="the artnet device's id, instead of finding it by host"
    )
    return parser.parse_args(list(argv))


async def run(argv: Sequence[str], out: TextIO | None = None) -> int:
    """The whole command; returns its exit status. ``out`` defaults to stdout."""
    stream = out if out is not None else sys.stdout

    def say(line: str = "") -> None:
        print(line, file=stream, flush=True)

    args = parse_args(argv)
    base_url = str(args.base_url).rstrip("/")
    say(
        f"Stage lighting provisioning: {base_url}"
        + (" (DRY RUN: nothing will change)" if args.dry_run else "")
    )
    try:
        if args.mint_admin_session:
            token, expires = mint_admin_token(args.config)
            say(f"Session: admin, minted on this machine, expires {expires}")
        else:
            token, expires = await login_with_password(base_url, args.password_env)
            say(f"Session: admin, signed in with ${args.password_env}, expires {expires}")
    except (CannotStart, httpx.HTTPError) as exc:
        say(f"Cannot start: {exc}")
        return EXIT_CANNOT_START

    async with httpx.AsyncClient(
        base_url=base_url, headers={"Cookie": f"{COOKIE_NAME}={token}"}, timeout=30.0
    ) as client:
        provisioner = Provisioner(
            Api(client), dmx_device_id=args.dmx_device_id, dmx_host=args.dmx_host
        )
        try:
            plan = await provisioner.plan()
        except (ApiFailure, httpx.HTTPError) as exc:
            say(f"Cannot read the installation: {exc}")
            return EXIT_ERROR

        say()
        say("Found")
        for line in plan.found:
            say(f"  {line}")
        if plan.notes:
            say()
            say("Notes")
            for line in plan.notes:
                say(f"  {line}")
        if plan.conflicts:
            say()
            say("CONFLICTS (nothing has been changed)")
            for line in plan.conflicts:
                say(f"  {line}")
            say()
            say(f"Stopped: {len(plan.conflicts)} conflict(s) to resolve by hand first.")
            return EXIT_CONFLICT
        say()
        if not plan.steps:
            say("Nothing to do: the stage lighting is already provisioned as specified.")
            return EXIT_OK
        say("Would do" if args.dry_run else "Doing")
        for step in plan.steps:
            say(f"  {step.verb:<6} {step.text}")
        creates = sum(1 for s in plan.steps if s.verb == "create")
        deletes = sum(1 for s in plan.steps if s.verb == "delete")
        changes = len(plan.steps) - creates - deletes
        deleting = f", {deletes} to delete" if deletes else ""
        if args.dry_run:
            say()
            say(
                f"Dry run: {creates} to create, {changes} to change{deleting}. "
                "Nothing was changed."
            )
            return EXIT_OK

        done = 0
        for step in plan.steps:
            try:
                await step.run()
            except (ApiFailure, httpx.HTTPError, KeyError) as exc:
                say()
                say(f"FAILED at: {step.verb} {step.text}")
                say(f"  {exc}")
                say(f"{done} of {len(plan.steps)} steps were done; run again to finish.")
                return EXIT_ERROR
            done += 1
        overlaps = await provisioner.patch_conflicts()
        say()
        if overlaps:
            say(f"WARNING: patch conflicts involve the stage fixtures: {overlaps}")
            return EXIT_ERROR
        deleted = f", {deletes} deleted" if deletes else ""
        say(f"Done: {creates} created, {changes} changed{deleted}. No patch conflicts.")
        return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    # The "all" bindings' names carry "→"; a console that cannot show it
    # (a C locale without UTF-8 mode, a Windows code page) must not stop a run.
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(errors="backslashreplace")
    return asyncio.run(run(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    raise SystemExit(main())
