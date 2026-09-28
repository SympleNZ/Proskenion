"""Which desk channels a mixer's channels cover (§7.3, §5.5)."""

from __future__ import annotations

from typing import Any

from proskenion.core.drivers.capabilities import ChannelRef, DeskChannel
from proskenion.core.mixer.desk_channels import declared_desk_channels, uncovered
from proskenion.db.crud.mixer import ChannelDriverRef, ChannelWithRefs, MixerChannel

MAIN = DeskChannel(ChannelRef("main", "Main LR", "main", True))
IP1 = DeskChannel(ChannelRef("ip1", "Input 1", "input", False))
OUT1 = DeskChannel(ChannelRef("out1", "Out 1", "output", False), ("out12",))
OUT2 = DeskChannel(ChannelRef("out2", "Out 2", "output", False), ("out12",))
DECLARED = [IP1, MAIN, OUT1, OUT2]


def channel(kind: str, refs: list[str], *, unmapped: bool = False) -> ChannelWithRefs:
    row = MixerChannel(
        id=len(refs),
        device_id=1,
        channel_kind=kind,
        name="x",
        short_name=None,
        notes=None,
        unmapped=unmapped,
        visible_staff=True,
        hirer_max_db=None,
        show_pan=False,
        tracked=True,
        sort_order=0,
        created_at="",
        updated_at="",
    )
    return ChannelWithRefs(row, [ChannelDriverRef(r, i) for i, r in enumerate(refs)])


def test_nothing_configured_leaves_every_desk_channel_missing_in_order() -> None:
    assert uncovered(DECLARED, []) == DECLARED


def test_a_reference_or_a_covering_reference_covers_a_desk_channel() -> None:
    existing = [channel("input", ["ip1"]), channel("output", ["out12"])]
    assert uncovered(DECLARED, existing) == [MAIN]


def test_a_ganged_channel_covers_each_of_its_references() -> None:
    assert uncovered(DECLARED, [channel("output", ["out1", "out2"])]) == [IP1, MAIN]


def test_an_unmapped_channel_covers_nothing_but_still_counts_as_the_main() -> None:
    existing = [channel("input", ["ip1"], unmapped=True), channel("main", ["m"], unmapped=True)]
    assert uncovered(DECLARED, existing) == [IP1, OUT1, OUT2]


class _RefsOnly:
    def available_refs(self) -> list[ChannelRef]:
        return [MAIN.ref, IP1.ref]


class _Declares(_RefsOnly):
    def desk_channels(self) -> list[DeskChannel]:
        return [IP1]


def test_a_driver_without_desk_channels_has_one_per_available_reference() -> None:
    driver: Any = _RefsOnly()
    assert declared_desk_channels(driver) == [MAIN, DeskChannel(IP1.ref)]
    declares: Any = _Declares()
    assert declared_desk_channels(declares) == [IP1]
