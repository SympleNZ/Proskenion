/*
 * The DMX channel map (spec §21.18 *Channel map*): a linear 512-channel
 * strip for one universe, with a universe picker when more than one is
 * patched. `buildChannelMap`/`patchedUniverses` (`./channelMap.ts`) do the
 * arithmetic; this renders it. Every cell is reachable by keyboard through a
 * single roving tab stop — 512 individual tab stops would make Tab useless
 * for reaching anything past the map (§24.2's composite-widget pattern, the
 * same one the stage plan's own roving fixtures use).
 */
import { useMemo, useState, type KeyboardEvent } from "react";

import { Banner } from "@/components/ui/Banner";
import { Select } from "@/components/ui/Select";
import { FieldLabel } from "@/help/HelpButton";
import type { Device } from "@/admin/devices/types";
import type { FixtureProfile, LightingChannel } from "@/lighting/types";

import { buildChannelMap, patchedUniverses, UNIVERSE_SLOT_COUNT, type ChannelMapCell } from "./dmxChannelMap";

export interface ChannelMapProps {
  channels: readonly LightingChannel[];
  profiles: readonly FixtureProfile[];
  devices: readonly Device[];
}

/** Twelve identity colours (§21.3's group palette), cycled by fixture id — identity only, never status. `components.css` maps each index to a `--group-*` token. */
const FIXTURE_COLOUR_COUNT = 12;

function cellLabel(cell: ChannelMapCell): string {
  if (cell.conflict && cell.channelName) return `Channel ${cell.slot}, conflict, ${cell.channelName}`;
  if (cell.conflict) return `Channel ${cell.slot}, conflict`;
  if (cell.channelName) return `Channel ${cell.slot}, ${cell.channelName}`;
  return `Channel ${cell.slot}, free`;
}

export function ChannelMap({ channels, profiles, devices }: ChannelMapProps) {
  const universes = useMemo(
    () => patchedUniverses(channels, (id) => devices.find((d) => d.id === id)?.name ?? `Device ${id}`),
    [channels, devices],
  );
  const [selectedKey, setSelectedKey] = useState<string | undefined>(universes[0]?.key);
  const active = universes.find((u) => u.key === selectedKey) ?? universes[0];
  const [focusedSlot, setFocusedSlot] = useState(1);

  const { cells, conflictingFixtures } = useMemo(
    () => (active ? buildChannelMap(channels, profiles, active.deviceId, active.universe) : { cells: [], conflictingFixtures: [] }),
    [active, channels, profiles],
  );

  function handleKeyDown(event: KeyboardEvent<HTMLButtonElement>, slot: number): void {
    let next: number;
    if (event.key === "ArrowRight") next = Math.min(UNIVERSE_SLOT_COUNT, slot + 1);
    else if (event.key === "ArrowLeft") next = Math.max(1, slot - 1);
    else if (event.key === "Home") next = 1;
    else if (event.key === "End") next = UNIVERSE_SLOT_COUNT;
    else return;
    event.preventDefault();
    setFocusedSlot(next);
    const el = document.querySelector<HTMLButtonElement>(`[data-channel-map-slot="${next}"]`);
    el?.focus();
  }

  if (universes.length === 0) {
    return <p className="text-fg-muted text-sm">No DMX fixture is patched to any universe yet.</p>;
  }

  return (
    <div className="flex flex-col gap-4">
      {universes.length > 1 ? (
        <div className="field">
          <FieldLabel htmlFor="channel-map-universe" help="lighting.channel-map.universe">
            Universe
          </FieldLabel>
          <Select id="channel-map-universe" value={selectedKey} onChange={(event) => setSelectedKey(event.currentTarget.value)}>
            {universes.map((u) => (
              <option key={u.key} value={u.key}>
                {u.deviceName} · Universe {u.universe}
              </option>
            ))}
          </Select>
        </div>
      ) : (
        <h3 className="section-title">
          DMX channel map · {active?.deviceName} · Universe {active?.universe}
        </h3>
      )}

      {conflictingFixtures.length > 0 ? (
        <Banner tone="danger" title="Patch conflict">
          Overlapping addresses: {conflictingFixtures.join(", ")}.
        </Banner>
      ) : null}

      <div className="channel-map-grid" role="group" aria-label={`DMX channel occupancy, universe ${active?.universe}`}>
        {cells.map((cell) => (
          <button
            key={cell.slot}
            type="button"
            data-channel-map-slot={cell.slot}
            className="channel-map-cell"
            data-state={cell.conflict ? "conflict" : cell.channelId !== null ? "occupied" : "free"}
            data-fixture-index={cell.channelId !== null && !cell.conflict ? cell.channelId % FIXTURE_COLOUR_COUNT : undefined}
            aria-label={cellLabel(cell)}
            tabIndex={cell.slot === focusedSlot ? 0 : -1}
            onFocus={() => setFocusedSlot(cell.slot)}
            onKeyDown={(event) => handleKeyDown(event, cell.slot)}
          >
            {cell.slot}
          </button>
        ))}
      </div>
      <p className="field-help">■ occupied · ▓ multi-channel fixture · ▨ conflict · □ free</p>
    </div>
  );
}
