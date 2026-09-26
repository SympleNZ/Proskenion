"""Universe buffers (spec §7.2.2, §7.2.3).

DMX512 does not address individual channels: every packet carries a whole
universe, up to 512 sequential values, and a fixture finds its channel by
counting bytes from the start. The application's job is to keep correct values
in a buffer per universe; the transport handles packet timing.

There is one :class:`UniverseBuffer` per ``(lighting output device, universe)``
pair that has at least one fixture patched to it.

One writer per output (§7.2.3)
------------------------------
The DMX pass — :meth:`proskenion.core.dmx.compositor.Compositor.composite_dmx`
— is the **sole writer** of every universe buffer. The buffers are created and
held privately by the compositor; nothing else is ever handed a
:class:`UniverseBuffer`. The frame renderer receives immutable ``bytes``
copies from :meth:`~proskenion.core.dmx.compositor.Compositor.frames` and can
therefore send a frame but never change one. Fades, scenes, direct sets and
bindings write the level store, never a buffer (§7.2.6).

Values here are DMX wire values, 0–255. This is the one place in the core where
they exist: the compositor converts the core's 0–100 levels at the boundary
(§9.2, B5).
"""

from __future__ import annotations

from dataclasses import dataclass

#: Slots in one DMX512 universe (§7.2.2).
UNIVERSE_SIZE = 512

#: The highest value a DMX slot carries.
DMX_MAX = 255


@dataclass(frozen=True, slots=True, order=True)
class UniverseKey:
    """Identifies one buffer: the lighting output device and its universe number."""

    device_id: int
    universe: int


def slot_index(address: int, offset: int) -> int:
    """The 0-based buffer index of a fixture channel.

    ``address`` is the fixture's DMX start address, 1-based as it is printed
    on the fixture and entered in the patch (1–512); ``offset`` is the
    profile channel's 0-based offset from that start (§15.9).
    """
    return address - 1 + offset


class UniverseBuffer:
    """512 bytes for one universe. Written only by the DMX pass (see the module docstring)."""

    __slots__ = ("_data", "key")

    def __init__(self, key: UniverseKey) -> None:
        self.key = key
        self._data = bytearray(UNIVERSE_SIZE)

    def clear(self) -> None:
        """Zero every slot. The DMX pass starts each composite from zero, so a
        fixture removed from the patch leaves its slots at zero rather than at
        their last value."""
        self._data[:] = bytes(UNIVERSE_SIZE)

    def write(self, index: int, value: int) -> bool:
        """Set one slot (0-based). The value is clamped to 0–255.

        Returns ``False`` without writing if ``index`` lies outside the
        universe — a fixture patched past slot 512 — so the caller can report
        it rather than raise mid-frame.
        """
        if not 0 <= index < UNIVERSE_SIZE:
            return False
        self._data[index] = min(max(int(value), 0), DMX_MAX)
        return True

    def read(self, index: int) -> int:
        """One slot's current value (0-based)."""
        return self._data[index]

    def frame(self) -> bytes:
        """An immutable copy of the whole universe — what a driver is sent."""
        return bytes(self._data)


__all__ = ["DMX_MAX", "UNIVERSE_SIZE", "UniverseBuffer", "UniverseKey", "slot_index"]
