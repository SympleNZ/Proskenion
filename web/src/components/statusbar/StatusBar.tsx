/*
 * Status bar (spec §21.7). Persistent along the bottom of every screen: one
 * indicator per device in the order KNX, DMX, Mixer, Projector, HDMI; then
 * the shared timer, the appliance clock and the account chip. Hirers get
 * indicators, the clock and a single Log out. Hidden below ~420 px of height
 * by components.css, when device state moves into the navigation rail.
 *
 * At a phone's width (owner's decision, 2026-09; not yet in the spec's own
 * §21.7 — see the coordinator's note), the five-dot row and the timer are
 * replaced by `StatusSummary`'s single worst-state LED; both structures are
 * always mounted and `components.css` picks one with a media query, exactly
 * how the phone label-drop below it already worked before this LED existed.
 */
import { useState } from "react";

import type { Tier } from "@/api/auth";
import { StatusDot } from "@/components/ui/StatusDot";
import { DEVICE_LABELS, DEVICE_ORDER, useDeviceStatus, type DeviceName } from "@/live/deviceStatus";

import { AccountChip } from "./AccountChip";
import { Clock } from "./Clock";
import { DeviceDetailSheet } from "./DeviceDetailSheet";
import { HirerLogout } from "./HirerLogout";
import { StatusSummary } from "./StatusSummary";
import { TimerControls } from "./TimerControls";

function DeviceIndicator({ name, onOpen }: { name: DeviceName; onOpen?: ((name: DeviceName) => void) | undefined }) {
  const { status } = useDeviceStatus(name);
  const label = DEVICE_LABELS[name];
  const body = (
    <>
      <StatusDot status={status} subject={label} />
      <span>{label}</span>
    </>
  );
  if (!onOpen) {
    return <span className="device-indicator">{body}</span>;
  }
  return (
    <button type="button" className="device-indicator" onClick={() => onOpen(name)} aria-label={`${label} details`}>
      {body}
    </button>
  );
}

export function StatusBar({ tier }: { tier: Tier }) {
  const [openDevice, setOpenDevice] = useState<DeviceName | null>(null);
  const staff = tier !== "hirer";

  return (
    <footer className="status-bar" aria-label="Status bar" data-testid="status-bar" data-tier={tier}>
      {/* Not a live region: a change is spoken once, briefly, by
          `ConnectionAnnouncer` (§24.3), not by re-reading this whole list.
          The accessible names stay for a user who navigates to it. */}
      <div className="status-bar-devices" role="list" aria-label="Devices">
        {DEVICE_ORDER.map((name) => (
          <div role="listitem" key={name}>
            <DeviceIndicator name={name} onOpen={staff ? setOpenDevice : undefined} />
          </div>
        ))}
      </div>
      <StatusSummary onOpen={staff ? setOpenDevice : undefined} />
      <div className="status-bar-right">
        {staff && (
          <>
            <TimerControls />
            <span className="status-sep" aria-hidden="true" />
          </>
        )}
        <Clock />
        <span className="status-sep" aria-hidden="true" />
        {staff ? <AccountChip tier={tier} /> : <HirerLogout />}
      </div>
      {staff && <DeviceDetailSheet name={openDevice} tier={tier} onClose={() => setOpenDevice(null)} />}
    </footer>
  );
}
