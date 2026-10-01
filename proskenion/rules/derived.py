"""Derived status — rule shape C (spec §8.2, §8.6, §7.1, §12.1, B51).

An outgoing KNX address that continuously reflects state::

    STATUS  knx 1/0/11  =  every fixture in "Row 1" at 100%
    STATUS  knx 4/0/4   =  every fixture in "Stage row 1" at 100%, as the room sees it
    STATUS  knx 1/2/2   =  projector state is ON
    STATUS  knx 1/0/9   =  external control active

**Derived, never written by whatever fired** (§8.6, B51). Nothing tells this
module "bank 1 is on". It watches the state store, and after any change it
recomputes every status from what the store holds and writes the ones whose
value differs from what it last wrote. That is why an All button reads 0 when
one bank is switched off on its own, why dragging a fixture down in the web
interface turns a panel indicator off, and why a lost telegram self-corrects
at the next change. Bindings hold no state of their own.

The three predicates
--------------------
``lighting_group_all_at``
    Every member channel is at ``compare_level``. A member whose own range
    cannot reach ``compare_level`` is compared with the nearest level it can
    hold, so a fixture capped at 80 % counts as "at 100 %" when it is at 80.
    A group with no members is never "all at" anything. What is compared is
    the status's ``basis`` (migration 011, owner decision 2026-09-30):

``level`` (the default, and §8.6 as written)
    Each member's **stored** level — what its fixture fader, its group fader
    and a binding's recall all set (a group fader sets levels; owner
    decision 2026-09-30).
``output`` ("what the room sees"; the stage panel's indicators)
    Each member's output as last actually composited into a frame
    (:meth:`~proskenion.core.dmx.compositor.Compositor.output_level`): its
    clamped level × the master, or, while a fader's operator glide is on its
    way, where the glide has reached. Every frame that moves a glide wakes a
    recompute, so "all at 100" turns true on the frame that lands it.
    With the master at 0 every stage row reads off, whatever its faders say.
    A KNX house dimmer is not scaled by the master, so its output is its
    level. A master change wakes a recompute only when an enabled status
    uses this basis, and is held for the frame that carries it, as a level
    change is.
``device_state``
    The device's connection status is in ``compare_state``
    (:func:`proskenion.rules.model.state_matches`). For the projector, also
    true when its own operational state (§7.4, ``state.projector.state``) is
    in ``compare_state`` — the two vocabularies are separate (§8.3) and
    either satisfies the status, which is why ``STATUS knx 1/2/2 = projector
    state is ON`` above works from ``state.projector`` rather than the
    projector's connection record.
``external_control``
    External control is active (§7.2.7).

Timing: after the frame, coalesced, on change of value
------------------------------------------------------
§7.1: "completion means the frame has been sent, not that the level store was
written" — the panel lights after the room does. A level change on a DMX
fixture marks its output device *pending* with the frame renderer's composite
count at that moment. The renderer calls back after each frame it sends
(:meth:`~proskenion.core.dmx.renderer.FrameRenderer.add_frame_listener`); a
frame with a larger count carries the change, and releases the device. A
status whose group touches a pending device is held — neither evaluated nor
written — until then. A level change on a KNX dimmer has no frame, so it is
recomputed on the change itself.

Recomputation runs in its own task, woken rather than called, so every change
made in one turn of the event loop is evaluated once, on the next (§8.6
*Coalescing*). A status is written only when its value differs from what this
module last wrote, at KNX priority 2. So a two-second fade to full emits one
telegram, after its last frame, rather than one per step.

External control (§7.2.7, §8.8)
-------------------------------
While it is active every status reflecting stage lighting — a
``lighting_group_all_at``, either basis, whose group contains a DMX fixture — reads 0, and
the ``external_control`` status 1, at once: no frame is coming. A group of KNX
house dimmers only is not stage lighting and is unaffected, as house lighting
always is. When external control ends, the stage statuses are held until the
first frame composited from the controller's own model has gone, then
recomputed and rewritten.

At startup (§12.1, §12.3)
-------------------------
Statuses are never persisted. :meth:`DerivedStatusEngine.start` evaluates
every status and writes each whose value differs from what was last written —
which, since nothing has been written yet, is every enabled one. The panel is
brought into line with the restored model after any restart.

No chaining (§8.7)
------------------
This module's only outputs are KNX writes and the ``binding_states`` map. It
never emits a bus event and never calls into the rule engine — a structural
test holds it to that — so a status write cannot look like a state change to
the rule layer. knxd's echo of the write is the other half, handled where
telegrams enter the rule layer (:mod:`proskenion.rules.engine`).

Re-assert after a panel press
-----------------------------
A KNX wall panel flips its own icon the moment a button is pressed, before
anything has happened. If the press then does not take effect — the projector
is unreachable or still warming (B52), a guard blocks the rule, the telegram
is debounced — no value changes, so nothing above would ever write, and the
panel would go on showing a state the room is not in. So once the rule layer
has finished with a telegram on a rule's trigger address, and with what that
telegram started (:meth:`proskenion.rules.engine.RulesEngine.handle_telegram`),
it calls :meth:`DerivedStatusEngine.reassert`: every enabled status with an
address is written again with its current value, even though it is unchanged.

The re-assert rides the ordinary recompute rather than writing on its own: it
marks the statuses and wakes the task, so the pass that writes them is the
same one that would have run anyway, evaluates from the store as it stands,
and keeps every timing rule above — a status held for its frame is written
when the frame releases it, not before. A status already written *since the
press* is skipped: that write reached the panel after it flipped its icon, so
the panel already shows the truth. A re-assert changes no value, so it
publishes nothing to the monitor or the lamps and cannot look like a state
change (below).

Button lamps (Q6, Phase 5 contracts "Button lamps") — the other output
------------------------------------------------------------------------
Every enabled status, KNX address or not, also writes its reading to
``state.status.lamps`` (owner ``"derived_status"``, B39) as
``{"on": bool | None, "transitioning": bool}``, in the same pass and under
the same coalescing as the KNX write — see :meth:`DerivedStatusEngine._write_lamps`.
A status with no address (Q6: "House at 100%", say, exists only to light a
page button) is evaluated and its lamp broadcast exactly like any other; it
is simply never handed to :attr:`DerivedStatusEngine._knx`.
``transitioning`` is true only for a ``device_state`` status whose device is
the projector and whose own operational state (§7.4) is currently
``warming`` or ``cooling`` — see :meth:`DerivedStatusEngine.transitioning`.

Bank states for the interface
-----------------------------
For each binding rule, whether every member of its group is at its
``on_level`` — false under external control for a stage group, matching the
panel — is written to ``state.lighting.binding_states`` keyed by rule id, in
the same pass and under the same timing as the statuses, and reaches the
browser as the ``bindings`` field of ``lighting_state`` frames.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from proskenion.core.dmx.compositor import LevelStoreView, LightingConfig, clamp_level
from proskenion.core.knx import Priority
from proskenion.core.lighting import LightingService
from proskenion.core.state import Change, StateStore
from proskenion.db.crud.base import now_iso
from proskenion.rules.model import state_matches

log = logging.getLogger(__name__)

#: The owner the rule layer registers on the lighting domain for ``binding_states``.
RULES_OWNER = "rules"
#: The ``state.status`` owner for ``lamps`` (B39) — see :class:`DerivedStatusEngine`.
LAMPS_OWNER = "derived_status"
#: §7.1: status feedback goes at priority 2.
STATUS_PRIORITY = Priority.SCENE_STATUS
#: Stored levels are one decimal; closer than this is "at".
LEVEL_TOLERANCE = 0.05
#: ``proskenion.core.devices.slot_name(Category.PROJECTOR)`` — duplicated as a
#: literal rather than imported, to keep this module from depending on the
#: device manager's category vocabulary for what is, structurally, just the
#: one device key a single-projector installation reports under (§7.4).
_PROJECTOR_SLOT = "projector"


class StatusWriter(Protocol):
    """The KNX subsystem's ``write`` as the derived statuses use it (§7.1)."""

    def write(
        self, group_address: str, value: Any, *, priority: Priority
    ) -> Awaitable[None] | None: ...


class DeviceKeys(Protocol):
    """Which ``state.devices`` key a device row reports under — the device manager."""

    def state_key(self, device_id: int) -> str | None: ...


@dataclass(frozen=True, slots=True)
class StatusSpec:
    """One ``derived_status`` row, with its group address resolved.

    ``group_address`` is ``None`` for a lamp-only status (Q6, migration 006):
    it is still evaluated and its lamp still broadcast on the ``status``
    frame, just never written to the KNX bus (§8.9).
    """

    id: int
    name: str
    enabled: bool
    group_address: str | None
    source_type: str
    lighting_group_id: int | None = None
    compare_level: float | None = None
    device_id: int | None = None
    compare_state: str | None = None
    #: ``level`` (stored) or ``output`` (composited) — ``lighting_group_all_at`` only.
    basis: str = "level"


@dataclass(frozen=True, slots=True)
class BindingSpec:
    """One binding rule, as the interface's bank state needs it."""

    rule_id: int
    lighting_group_id: int
    on_level: float


@dataclass(frozen=True, slots=True)
class StatusReading:
    """One row of §8.10's live monitor and ``GET /derived-status/state``."""

    id: int
    name: str
    group_address: str | None
    """``None`` for a lamp-only status (Q6): evaluated and broadcast like any
    other, just never written to the KNX bus."""
    source_type: str
    enabled: bool
    value: bool | None
    """The current evaluation; ``None`` while disabled or not yet evaluated."""
    written: bool | None
    """What was last written to the bus; ``None`` if nothing has been."""
    changed_at: str | None
    """When ``value`` last changed, ISO 8601 with offset (§4.9)."""
    held: bool
    """Waiting for the frame that carries a change (§7.1)."""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class DerivedStatusEngine:
    """Recomputes every derived status and bank state from the state store."""

    def __init__(
        self,
        state: StateStore,
        *,
        lighting: LightingService | None = None,
        knx: StatusWriter | None = None,
        devices: DeviceKeys | None = None,
        clock: Callable[[], str] = now_iso,
    ) -> None:
        self._state = state
        self._lighting = lighting
        self._knx = knx
        self._devices = devices
        self._now = clock
        self._view = LevelStoreView(state.lighting)
        self._statuses: tuple[StatusSpec, ...] = ()
        self._bindings: tuple[BindingSpec, ...] = ()
        self._written: dict[int, bool] = {}
        self._written_to: dict[int, str] = {}
        self._values: dict[int, bool] = {}
        self._changed_at: dict[int, str] = {}
        self._held: set[int] = set()
        self._pending: dict[int, int] = {}
        self._glide_frame_seen = 0
        #: Statuses to write at the next pass even if unchanged, each with the
        #: loop time of the latest press that asked — see :meth:`reassert`.
        self._reassert: dict[int, float] = {}
        #: Loop time of each status's last successful KNX write.
        self._written_at: dict[int, float] = {}
        self._warned: set[int] = set()
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._subscribers: set[asyncio.Queue[StatusReading]] = set()
        self._writer_registered = False
        self._lamps_writer_registered = False
        self._channel_cache: tuple[LightingConfig | None, dict[int, int]] = (None, {})
        self.passes = 0
        """Evaluation passes run — tests read it."""
        self.writes = 0
        """Status telegrams handed to the KNX subsystem."""
        self.reasserts = 0
        """Of :attr:`writes`, those that re-sent an unchanged value (:meth:`reassert`)."""

    # -- configuration -------------------------------------------------------

    def configure(self, statuses: Iterable[StatusSpec], bindings: Iterable[BindingSpec]) -> None:
        """Adopt the ``derived_status`` rows and the binding rules; recompute."""
        self._statuses = tuple(statuses)
        self._bindings = tuple(bindings)
        current = {s.id: s for s in self._statuses}
        for table in (
            self._written,
            self._values,
            self._changed_at,
            self._reassert,
            self._written_at,
        ):
            for status_id in [k for k in table if k not in current]:
                del table[status_id]
        for status_id, address in list(self._written_to.items()):
            spec = current.get(status_id)
            if spec is None or spec.group_address != address:
                # A new address has never been written: the next pass writes it.
                self._written.pop(status_id, None)
                self._written_at.pop(status_id, None)
                del self._written_to[status_id]
        for status_id in [k for k, s in current.items() if not s.enabled]:
            self._values.pop(status_id, None)
        self._wake.set()

    @property
    def statuses(self) -> tuple[StatusSpec, ...]:
        return self._statuses

    @property
    def addresses(self) -> frozenset[str]:
        """Every configured status address. The rule layer never triggers on one.

        A lamp-only status (Q6) contributes nothing here: it has no address
        to echo back and so needs no echo guard.
        """
        return frozenset(s.group_address for s in self._statuses if s.group_address is not None)

    # -- lifecycle -------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        """Start watching, then §12.1: evaluate every status and write those that differ."""
        if self.running:
            return
        self._state.add_listener(self._on_change)
        if self._lighting is not None:
            self._lighting.renderer.add_frame_listener(self._on_frame)
        self._pending.clear()
        await self._pass()
        self._task = asyncio.get_running_loop().create_task(self._run(), name="derived-status")

    async def stop(self) -> None:
        self._state.remove_listener(self._on_change)
        if self._lighting is not None:
            self._lighting.renderer.remove_frame_listener(self._on_frame)
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    # -- re-assert after a press -----------------------------------------------

    def reassert(self, *, since: float | None = None) -> None:
        """Write every enabled status with an address again, even if unchanged.

        Called by the rule layer once it has finished with a telegram on a
        rule's trigger address, so a panel that flipped its own icon on a
        press that did not take effect is corrected (see the module
        docstring). It does not write here: it marks the statuses and wakes
        the recompute task, whose next pass writes each at its current value
        through the ordinary path, at the ordinary priority. A status held
        for its frame stays marked and is written when the frame releases it.

        ``since`` is the loop time (``loop.time()``) of the latest press being
        answered: a status successfully written at or after it is skipped,
        since that write reached the panel after its icon flipped. ``None``
        re-sends every one.
        """
        mark = float("-inf") if since is None else since
        for spec in self._statuses:
            if spec.enabled and spec.group_address is not None:
                self._reassert[spec.id] = max(self._reassert.get(spec.id, mark), mark)
        self._wake.set()

    # -- change intake (synchronous, on every store write) -------------------

    def _on_change(self, change: Change) -> None:
        if change.domain == "devices":
            self._wake.set()
            return
        if change.domain == "projector":
            if change.field == "state":
                # The lamp frame's ``transitioning`` flag (§7.4, §21.9) tracks
                # the projector's own warming/cooling state directly; a
                # device_state status bound to it needs recomputing too.
                self._wake.set()
            return
        if change.domain != "lighting":
            return
        if change.field == "levels":
            device = self._device_of(change.item)
            if device is not None and self._frames_flowing():
                # Released by the frame that carries this change (§7.1).
                self._pending[device] = self._frame_count()
                return
            self._wake.set()  # a KNX dimmer: no frame, recompute on the change
        elif change.field == "external_control":
            self._on_external_control()
            self._wake.set()
        elif change.field == "master" and self._output_basis_in_use():
            self._on_master_change()

    def _output_basis_in_use(self) -> bool:
        return any(s.enabled and s.basis == "output" for s in self._statuses)

    def _on_master_change(self) -> None:
        """The master moved: an ``output`` status may change.

        Only DMX fixtures are scaled (a house dimmer is not, §9.5), so every
        device with a fixture patched is held for the frame carrying the
        change (§7.1), exactly as a level change is. With no frames flowing
        there is no frame to wait for: recompute now. (Groups no longer
        scale — a group fader writes levels — so the master is the only
        scaling input; owner decision 2026-09-30.)
        """
        if self._lighting is None:
            return
        devices = set(self._channel_devices().values())
        if not devices:
            return
        if self._frames_flowing():
            count = self._frame_count()
            for device in devices:
                self._pending[device] = count
            return
        self._wake.set()

    def _on_external_control(self) -> None:
        if self._lighting is None:
            return
        if self._lighting.external_active:
            # Nothing will be sent; the stage statuses read 0 now.
            self._pending.clear()
            return
        # Resuming: hold the stage statuses for the first frame composited from
        # the controller's own model (§7.2.7 *Resuming*).
        if not self._lighting.renderer.running:
            return
        count = self._frame_count()
        for device in {c.device_id for c in self._lighting.config.dmx_channels}:
            self._pending[device] = count

    def _on_frame(self, device_id: int, frame: int) -> None:
        if self._lighting is not None and self._lighting.config is not self._channel_cache[0]:
            # The lighting configuration was reloaded (groups, patch) — possibly
            # after this module last looked, since the two reloads race. Its
            # first frame is the cue to recompute against the new one.
            self._wake.set()
        seen = self._pending.get(device_id)
        if seen is not None and frame > seen:
            del self._pending[device_id]
            self._wake.set()
        if self._lighting is not None and self._output_basis_in_use():
            # An operator glide moved the output without a store change:
            # an ``output`` status reads the frame, so look again.
            glide_frame = self._lighting.renderer.last_glide_composite
            if glide_frame == frame and glide_frame != self._glide_frame_seen:
                self._glide_frame_seen = glide_frame
                self._wake.set()

    def _frames_flowing(self) -> bool:
        lighting = self._lighting
        return (
            lighting is not None and lighting.renderer.running and not lighting.renderer.suspended
        )

    def _frame_count(self) -> int:
        return 0 if self._lighting is None else self._lighting.renderer.composites

    def _device_of(self, item: str | None) -> int | None:
        if item is None or self._lighting is None:
            return None
        try:
            channel_id = int(item)
        except ValueError:
            return None
        return self._channel_devices().get(channel_id)

    def _channel_devices(self) -> dict[int, int]:
        """``{DMX channel id: output device id}`` for the current configuration."""
        assert self._lighting is not None
        config = self._lighting.config
        cached_for, mapping = self._channel_cache
        if cached_for is not config:
            mapping = {c.id: c.device_id for c in config.dmx_channels}
            self._channel_cache = (config, mapping)
        return mapping

    # -- evaluation --------------------------------------------------------------

    async def _run(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            try:
                await self._pass()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("derived status evaluation failed")

    async def evaluate(self) -> None:
        """Run one pass now — what the woken task does."""
        await self._pass()

    async def _pass(self) -> None:
        self.passes += 1
        if self._lighting is not None:
            self._channel_devices()  # adopt the current configuration
        pending = set(self._pending)
        changed: list[StatusSpec] = []
        to_write: list[tuple[StatusSpec, bool, bool]] = []
        self._held = set()
        for spec in self._statuses:
            if not spec.enabled or spec.group_address is None:
                self._reassert.pop(spec.id, None)
            if not spec.enabled:
                continue
            if pending and self._devices_of_group(spec.lighting_group_id) & pending:
                self._held.add(spec.id)  # a re-assert stays marked until released
                continue
            value = self._evaluate(spec)
            if self._values.get(spec.id) != value:
                self._values[spec.id] = value
                self._changed_at[spec.id] = self._now()
                changed.append(spec)
            since = self._reassert.pop(spec.id, None)
            if self._written.get(spec.id) != value:
                to_write.append((spec, value, False))
            elif since is not None and self._written_at.get(spec.id, float("-inf")) < since:
                to_write.append((spec, value, True))
        self._write_binding_states(pending)
        for spec, value, again in to_write:
            await self._write(spec, value, reassert=again)
        self._write_lamps()
        for spec in changed:
            self._publish(self._reading(spec))

    def _evaluate(self, spec: StatusSpec) -> bool:
        if spec.source_type == "external_control":
            return self.external_active
        if spec.source_type == "lighting_group_all_at":
            if spec.lighting_group_id is None or spec.compare_level is None:
                return False
            return self.group_at(spec.lighting_group_id, spec.compare_level, basis=spec.basis)
        if spec.source_type == "device_state":
            if spec.device_id is None or spec.compare_state is None:
                return False
            if state_matches(spec.compare_state, self.device_status(spec.device_id)):
                return True
            if self._is_projector(spec.device_id):
                return state_matches(spec.compare_state, self._projector_state())
            return False
        return False

    @property
    def external_active(self) -> bool:
        return self._lighting is not None and self._lighting.external_active

    def group_members(self, group_id: int) -> frozenset[int] | None:
        """A group's members in the lighting configuration, or ``None`` if unknown."""
        if self._lighting is None:
            return None
        return self._lighting.config.groups.get(group_id)

    def group_touches_dmx(self, group_id: int) -> bool:
        """Whether a group contains a DMX fixture — stage lighting, in §7.2.7's terms."""
        members = self.group_members(group_id)
        if not members or self._lighting is None:
            return False
        return not members.isdisjoint(self._lighting.compositor.dmx_channel_ids)

    def group_suppressed(self, group_id: int) -> bool:
        """External control holds this group: its binding is suppressed, its status 0."""
        return self.external_active and self.group_touches_dmx(group_id)

    def group_at(self, group_id: int, level: float, *, basis: str = "level") -> bool:
        """Every member at ``level`` — 0 under external control (§8.8).

        ``basis`` is ``level`` (the stored level) or ``output`` (what the room
        sees: level × the master).
        """
        members = self.group_members(group_id)
        if not members or self.group_suppressed(group_id):
            return False
        assert self._lighting is not None
        ranges = self._lighting.config.ranges()
        for channel_id in members:
            low, high = ranges.get(channel_id, (0.0, 100.0))
            target = clamp_level(level, low, high)
            if abs(self._member_value(channel_id, basis) - target) > LEVEL_TOLERANCE:
                return False
        return True

    def _member_value(self, channel_id: int, basis: str) -> float:
        """The stored level, or with ``basis == "output"`` the composited output.

        A member the lighting configuration cannot drive (not fully patched)
        sends nothing, so its output is 0.
        """
        if basis == "output":
            assert self._lighting is not None
            if self._frames_flowing():
                output = self._lighting.output_level(channel_id)  # what went, mid-glide too
            else:
                output = self._lighting.composited_level(channel_id)
            return 0.0 if output is None else output
        return self._view.level(channel_id)

    def device_status(self, device_id: int) -> str | None:
        key = None if self._devices is None else self._devices.state_key(device_id)
        record = None if key is None else self._state.devices.record(key)
        return None if record is None else record.status

    def _is_projector(self, device_id: int) -> bool:
        return self._devices is not None and self._devices.state_key(device_id) == _PROJECTOR_SLOT

    def _projector_state(self) -> str | None:
        value = self._state.projector.get("state")
        return value if isinstance(value, str) else None

    def transitioning(self, spec: StatusSpec) -> bool:
        """The lamp frame's ``transitioning`` flag (§7.4, §21.9's amber pulse).

        True only for a ``device_state`` status whose device is the
        projector and whose projector's own operational state
        (``state.projector.state``, §7.4) is currently ``warming`` or
        ``cooling`` — independent of what the status's own ``compare_state``
        is, and independent of :meth:`_evaluate`'s boolean reading. Any other
        status is never transitioning.
        """
        if spec.source_type != "device_state" or spec.device_id is None:
            return False
        if not self._is_projector(spec.device_id):
            return False
        return self._projector_state() in ("warming", "cooling")

    def _devices_of_group(self, group_id: int | None) -> set[int]:
        if group_id is None or self._lighting is None:
            return set()
        members = self.group_members(group_id) or frozenset()
        channels = self._channel_devices()
        return {channels[c] for c in members if c in channels}

    # -- bank states (§7.1, §21.11) ----------------------------------------------

    def binding_states(self) -> dict[str, bool]:
        """``state.lighting.binding_states`` as it stands."""
        current = self._state.lighting.get("binding_states")
        if not isinstance(current, Mapping):
            return {}
        return {str(k): bool(v) for k, v in current.items()}

    def _write_binding_states(self, pending: set[int]) -> None:
        previous = self.binding_states()
        states: dict[str, bool] = {}
        for binding in self._bindings:
            key = str(binding.rule_id)
            if pending and self._devices_of_group(binding.lighting_group_id) & pending:
                if key in previous:
                    states[key] = previous[key]
                    continue
            states[key] = self.group_at(binding.lighting_group_id, binding.on_level)
        if states == previous:
            return
        if not self._writer_registered:
            try:
                self._state.register_owner("lighting", RULES_OWNER, allow_multiple=True)
            except ValueError:
                log.error("the lighting domain is not shared; bank states cannot be published")
                return
            self._writer_registered = True
        self._state.lighting.writer(RULES_OWNER).set("binding_states", states)

    # -- button lamps (Phase 5 contracts, "Button lamps") -------------------------

    def _lamp_entry(self, spec: StatusSpec) -> dict[str, object]:
        return {
            "on": self._values.get(spec.id) if spec.enabled else None,
            "transitioning": self.transitioning(spec),
        }

    def _write_lamps(self) -> None:
        """``state.status.lamps`` as of this pass: every configured status,
        keyed by its id — including a disabled one, whose lamp reads
        ``{"on": null, ...}`` rather than simply vanishing.

        A whole-map ``set`` diffs item by item (§5.6), so this is safe to
        call on every pass: nothing is dirtied, and no ``LampsChanged`` event
        fires, unless a lamp's reading actually changed.
        """
        lamps = {str(spec.id): self._lamp_entry(spec) for spec in self._statuses}
        if not self._lamps_writer_registered:
            try:
                self._state.register_owner("status", LAMPS_OWNER)
            except ValueError:
                log.error("the status domain is not shared; lamps cannot be published")
                return
            self._lamps_writer_registered = True
        self._state.status.writer(LAMPS_OWNER).set("lamps", lamps)

    # -- writing -------------------------------------------------------------------

    async def _write(self, spec: StatusSpec, value: bool, *, reassert: bool = False) -> None:
        if spec.group_address is None:
            # Q6: a lamp-only status has nothing to write to the KNX bus —
            # no telegram is sent and self.writes (KNX telegrams) is
            # unaffected — but it is still "written" in the sense that this
            # pass's value is now the last one applied, so the next pass
            # does not retry it and the lamp still updates via
            # :meth:`_write_lamps`.
            self._written[spec.id] = value
            return
        if self._knx is None:
            return
        try:
            pending = self._knx.write(spec.group_address, value, priority=STATUS_PRIORITY)
            if inspect.isawaitable(pending):
                await pending
        except asyncio.CancelledError:
            raise
        except Exception:
            if spec.id not in self._warned:
                self._warned.add(spec.id)
                log.exception(
                    "derived status could not be written",
                    extra={"status_id": spec.id, "group_address": spec.group_address},
                )
            return
        self._warned.discard(spec.id)
        self._written[spec.id] = value
        self._written_to[spec.id] = spec.group_address
        self._written_at[spec.id] = asyncio.get_running_loop().time()
        self.writes += 1
        if reassert:
            self.reasserts += 1
        log.debug(
            "derived status re-asserted" if reassert else "derived status written",
            extra={"status_id": spec.id, "group_address": spec.group_address, "value": value},
        )

    # -- the live monitor (§8.10) --------------------------------------------------

    def _reading(self, spec: StatusSpec) -> StatusReading:
        return StatusReading(
            id=spec.id,
            name=spec.name,
            group_address=spec.group_address,
            source_type=spec.source_type,
            enabled=spec.enabled,
            value=self._values.get(spec.id) if spec.enabled else None,
            written=self._written.get(spec.id),
            changed_at=self._changed_at.get(spec.id),
            held=spec.id in self._held,
        )

    def readings(self) -> list[StatusReading]:
        """Every configured status: its address, current value and when it changed."""
        return [self._reading(spec) for spec in self._statuses]

    def _publish(self, reading: StatusReading) -> None:
        for queue in list(self._subscribers):
            if queue.full():
                queue.get_nowait()  # a slow reader sees the newest, not a backlog
            queue.put_nowait(reading)

    async def stream(
        self, *, idle_s: float | None = None, queue_size: int = 64
    ) -> AsyncIterator[StatusReading | None]:
        """Every status as it stands, then each change of value as it happens.

        With ``idle_s``, ``None`` is yielded after that long with nothing to
        report, so a server-sent events stream can send a keepalive.
        """
        queue: asyncio.Queue[StatusReading] = asyncio.Queue(maxsize=queue_size)
        for reading in self.readings()[-queue_size:]:
            queue.put_nowait(reading)
        self._subscribers.add(queue)
        try:
            while True:
                try:
                    async with asyncio.timeout(idle_s):
                        reading = await queue.get()
                except TimeoutError:
                    yield None
                    continue
                yield reading
        finally:
            self._subscribers.discard(queue)


__all__ = [
    "LAMPS_OWNER",
    "LEVEL_TOLERANCE",
    "RULES_OWNER",
    "STATUS_PRIORITY",
    "BindingSpec",
    "DerivedStatusEngine",
    "DeviceKeys",
    "StatusReading",
    "StatusSpec",
    "StatusWriter",
]
