"""A mixer channel for every channel on the desk (§7.3, §5.5, §21.21).

Every desk channel exists in admin by default: what a hirer sees is decided
by the pages they are given, not by which channels exist (§15.4, B61). So a
mixer device is given one channel per desk channel its driver declares, in
the driver's own order, when it is added — by ``POST /devices`` or the
first-run wizard — and an admin can later add any the device lacks with
"Add missing channels".

**What a desk channel is** comes from the driver's optional
``desk_channels()``
(:class:`~proskenion.core.drivers.capabilities.DeskChannel`); a driver
without one has a desk channel per ``available_refs()`` entry. A stereo
source the driver reports as one stereo reference (the CQ-20B's ST1, ST2,
USB and Bluetooth, and Main LR) becomes one channel with that one
reference. Outputs are created one each; a linked pair is the admin's
statement (§7.3) and is chosen afterwards by pointing a channel at the pair.

**A created channel** takes the reference's label as its name and its kind
as the channel's kind. It is visible to staff, has no hirer ceiling, keeps
pan hidden and is tracked: the same defaults a channel added by hand gets.
The admin renames it, hides it from staff, or deletes it.

**What counts as covered.** A desk channel is covered when any channel on
the device holds its reference or one of the references in its
``covered_by``. A channel left unmapped by a driver change does not count:
the references it keeps are the old driver's, kept only to say what it used
to be, and may happen to share a name with the new driver's. Main is covered
by any channel of kind ``main``, mapped or not: there is only ever one
(``idx_mixer_channels_one_main``).

**Adding never edits.** :func:`add_missing_channels` creates only what is not
covered, after the existing channels in ``sort_order``, and never renames,
reorders, re-points or deletes an existing channel. Run twice, the second run
creates nothing.

**A driver change** does not add channels by itself: the re-mapping screen
owns that moment (§5.5). Once references are re-mapped it is told how many
desk channels no channel covers, and offers to add them.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING

from proskenion.core.devices import DeviceUnavailable
from proskenion.core.drivers.base import Driver
from proskenion.core.drivers.capabilities import ChannelRef, DeskChannel
from proskenion.core.drivers.categories import Category
from proskenion.db.connection import Database
from proskenion.db.crud import mixer as mixer_crud
from proskenion.db.crud.devices import Device

if TYPE_CHECKING:
    from proskenion.core.devices import DeviceManager

log = logging.getLogger(__name__)

MAIN_KIND = "main"

#: Held from reading what exists to writing what is missing, so two requests
#: at once cannot both decide the same channel is missing.
_adding = asyncio.Lock()


def declared_desk_channels(driver: Driver) -> list[DeskChannel]:
    """The driver's ``desk_channels()``, or one per ``available_refs()``
    entry for a driver that declares none."""
    desk_channels = getattr(driver, "desk_channels", None)
    if desk_channels is not None:
        declared = desk_channels()
        return [d for d in declared if isinstance(d, DeskChannel)]
    available_refs = getattr(driver, "available_refs", None)
    if available_refs is None:
        return []
    refs = available_refs()
    if not isinstance(refs, list):
        return []
    return [DeskChannel(ref) for ref in refs if isinstance(ref, ChannelRef)]


def uncovered(
    declared: Sequence[DeskChannel], existing: Sequence[mixer_crud.ChannelWithRefs]
) -> list[DeskChannel]:
    """The desk channels in ``declared`` that no channel in ``existing``
    covers, in declared order (see the module docstring)."""
    has_main = any(c.channel.channel_kind == MAIN_KIND for c in existing)
    held = {r.driver_ref for c in existing if not c.channel.unmapped for r in c.refs}
    missing: list[DeskChannel] = []
    for desk in declared:
        if desk.ref.kind == MAIN_KIND:
            if not has_main:
                missing.append(desk)
            continue
        if desk.ref.ref in held or any(ref in held for ref in desk.covered_by):
            continue
        missing.append(desk)
    return missing


async def _driver_for(manager: DeviceManager, device: Device) -> Driver | None:
    if device.category != Category.MIXER.value:
        return None
    try:
        driver, _as_connected = await manager.resolve_driver(device.id)
    except DeviceUnavailable:
        log.warning("mixer device %s: could not resolve a driver to list its channels", device.id)
        return None
    return driver


async def missing_channels(
    db: Database, manager: DeviceManager, device: Device
) -> list[DeskChannel]:
    """The desk channels ``device`` has no channel for; empty for a device
    that is not a mixer or whose driver cannot be resolved."""
    driver = await _driver_for(manager, device)
    if driver is None:
        return []
    existing = await mixer_crud.list_channels_with_refs(db, device.id)
    return uncovered(declared_desk_channels(driver), existing)


async def add_missing_channels(
    db: Database, manager: DeviceManager, device: Device
) -> list[mixer_crud.MixerChannel]:
    """Create a channel for every desk channel ``device`` lacks, in one
    transaction, after its existing channels; return what was created.

    Nothing is created for a device that is not a mixer, or whose driver
    cannot yet be resolved (a later call can try again). The caller emits
    :class:`~proskenion.core.events.MixerConfigChanged` when this returns
    anything.
    """
    driver = await _driver_for(manager, device)
    if driver is None:
        return []
    declared = declared_desk_channels(driver)
    async with _adding:
        existing = await mixer_crud.list_channels_with_refs(db, device.id)
        missing = uncovered(declared, existing)
        if not missing:
            return []
        start = max((c.channel.sort_order for c in existing), default=-1) + 1
        return await mixer_crud.create_channels(
            db,
            device.id,
            [
                mixer_crud.NewChannel(
                    channel_kind=desk.ref.kind,
                    name=desk.ref.label,
                    driver_refs=(desk.ref.ref,),
                    sort_order=start + index,
                )
                for index, desk in enumerate(missing)
            ],
        )


__all__ = [
    "add_missing_channels",
    "declared_desk_channels",
    "missing_channels",
    "uncovered",
]
