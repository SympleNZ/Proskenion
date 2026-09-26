"""Re-mapping references after a driver change (spec §5.5 *Driver references
and swaps*, §21.24, §15.6).

``driver_ref`` is opaque to the core, and there is no automatic translation
between vendor addressing schemes, so a driver change invalidates every
reference the device's rows hold. The admin is shown each row's old
reference and name beside a picker of the new driver's ``available_refs()``
and chooses. Names, ceilings, visibility, group memberships, scene actions
and surface assignments are untouched: they point at the row, not the
reference.

The rules this module applies:

**Proposals are identity, never position.** A positional guess was
considered and rejected (§5.5): it would occasionally be silently wrong. The
one pre-selection offered is the *same* reference string where the new
driver declares it, with the same kind — swapping back to a driver the
channel was configured against — plus the new driver's Main for the Main
channel, of which every desk has exactly one. Everything else starts blank.
A proposal is only ever a starting position on a screen the admin confirms.

**The kind comes from the driver.** A mapped channel takes the new first
reference's ``ChannelRef.kind`` (§5.5 *Channel references*). The Main
channel can only map onto the new driver's Main, and no other channel can:
there is one Main per device and it is never removed (§7.3).

**Unmapped fails closed.** A mixer channel left without an equivalent for
any of its references is marked ``unmapped``: not controllable, and excluded
from hirer access until re-mapped (§15.6, §21.21). It keeps its old
references so the screen can still say what it used to be. A matrix input
or output has no ``unmapped`` column (§15.10), so every one must be mapped;
a re-map that leaves one out is refused rather than left pointing at a
reference the new driver may not have.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from proskenion.core.drivers.capabilities import ChannelRef, MatrixRefs
from proskenion.db.crud.remap import (
    MATRIX_INPUT,
    MATRIX_OUTPUT,
    MIXER_CHANNEL,
    Assignment,
    Holder,
    RefHolder,
)

MAIN_KIND = "main"


@dataclass(frozen=True, slots=True)
class Available:
    """The new driver's references, split by the holder that may use them."""

    #: A mixer's ``available_refs()``; empty for any other category.
    refs: tuple[ChannelRef, ...] = ()
    inputs: tuple[ChannelRef, ...] = ()
    outputs: tuple[ChannelRef, ...] = ()

    @classmethod
    def from_driver(cls, refs: object) -> Available:
        """From whatever ``available_refs()`` returned — a mixer's list or a
        matrix's :class:`MatrixRefs`."""
        if isinstance(refs, MatrixRefs):
            return cls(inputs=tuple(refs.inputs), outputs=tuple(refs.outputs))
        if isinstance(refs, Sequence):
            return cls(refs=tuple(r for r in refs if isinstance(r, ChannelRef)))
        return cls()

    def for_holder(self, holder: Holder) -> tuple[ChannelRef, ...]:
        if holder == MATRIX_INPUT:
            return self.inputs
        if holder == MATRIX_OUTPUT:
            return self.outputs
        return self.refs


@dataclass(frozen=True, slots=True)
class Proposal:
    """One row of the re-mapping screen: the holder and a proposal per old reference."""

    holder: RefHolder
    proposed: tuple[str | None, ...]


class RemapRejected(Exception):
    """The admin's choices cannot be applied; ``detail`` is per-row, §16.1 shaped."""

    def __init__(self, message: str, detail: dict[str, list[str]]) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


def _key(holder: Holder, row_id: int) -> str:
    return f"{holder}:{row_id}"


def propose(holders: Sequence[RefHolder], available: Available) -> list[Proposal]:
    """Every holder with a pre-selection per old reference (see the module
    docstring): the same reference with the same kind, or the new Main for
    the Main channel; otherwise ``None``."""
    mappings: list[Proposal] = []
    for holder in holders:
        candidates = available.for_holder(holder.holder)
        by_ref = {c.ref: c for c in candidates}
        main = next((c for c in candidates if c.kind == MAIN_KIND), None)
        proposed: list[str | None] = []
        for old in holder.refs:
            same = by_ref.get(old)
            if same is not None and (holder.holder != MIXER_CHANNEL or same.kind == holder.kind):
                proposed.append(same.ref)
            elif holder.holder == MIXER_CHANNEL and holder.kind == MAIN_KIND and main is not None:
                proposed.append(main.ref)
            else:
                proposed.append(None)
        if not holder.refs and holder.kind == MAIN_KIND and main is not None:
            proposed.append(main.ref)
        mappings.append(Proposal(holder=holder, proposed=tuple(proposed)))
    return mappings


def resolve(
    holders: Sequence[RefHolder],
    chosen: Mapping[tuple[Holder, int], Sequence[str] | None],
    available: Available,
) -> list[Assignment]:
    """The assignments to write for ``chosen``, or :class:`RemapRejected`.

    ``chosen`` maps ``(holder, id)`` to the new references in order, or to
    ``None``/empty to leave the row unmapped. A holder of this device that
    ``chosen`` does not name is left unmapped too. Naming a row that is not
    one of this device's holders is refused, as is any reference the new
    driver does not declare for that holder.
    """
    known = {(h.holder, h.id): h for h in holders}
    errors: dict[str, list[str]] = {}
    for holder_key in chosen:
        if holder_key not in known:
            errors.setdefault(_key(*holder_key), []).append(
                "this device holds no such row; reload the re-mapping screen"
            )

    assignments: list[Assignment] = []
    matrix_used: dict[Holder, dict[str, int]] = {MATRIX_INPUT: {}, MATRIX_OUTPUT: {}}
    for holder in holders:
        key = _key(holder.holder, holder.id)
        refs = tuple(chosen.get((holder.holder, holder.id)) or ())
        if not refs:
            if holder.holder != MIXER_CHANNEL:
                errors.setdefault(key, []).append(
                    f"{holder.name} must be mapped: a matrix {holder.kind} has no unmapped state"
                )
                continue
            assignments.append(Assignment(holder.holder, holder.id, None))
            continue
        by_ref = {c.ref: c for c in available.for_holder(holder.holder)}
        unknown = [ref for ref in refs if ref not in by_ref]
        if unknown:
            errors.setdefault(key, []).extend(
                f"the new driver has no reference {ref!r}" for ref in unknown
            )
            continue
        if len(set(refs)) != len(refs):
            errors.setdefault(key, []).append("a reference cannot appear twice on one row")
            continue
        if holder.holder == MIXER_CHANNEL:
            kind = by_ref[refs[0]].kind
            if (holder.kind == MAIN_KIND) != (kind == MAIN_KIND):
                errors.setdefault(key, []).append(
                    "the Main channel maps only to the new driver's Main, and nothing else can"
                )
                continue
            assignments.append(Assignment(holder.holder, holder.id, refs, kind))
            continue
        if len(refs) != 1:
            errors.setdefault(key, []).append(f"a matrix {holder.kind} takes exactly one reference")
            continue
        taken_by = matrix_used[holder.holder].get(refs[0])
        if taken_by is not None:
            errors.setdefault(key, []).append(
                f"{refs[0]!r} is already chosen for another {holder.kind}"
            )
            continue
        matrix_used[holder.holder][refs[0]] = holder.id
        assignments.append(Assignment(holder.holder, holder.id, refs))

    if errors:
        raise RemapRejected("Some of the re-mapping cannot be applied", errors)
    return assignments


__all__ = [
    "Available",
    "Proposal",
    "RemapRejected",
    "propose",
    "resolve",
]
