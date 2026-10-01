/*
 * The one place device-connection changes are spoken (spec §24.3: "Live
 * regions announce device status changes"; owner's Narrator test, 1 Oct 2026:
 * stopping knxd gave "KNX offline" buried in a lot of other reading).
 *
 * Before this, one device change fired several polite live regions at once:
 * the whole status-bar device list, the phone summary LED, the device-offline
 * banner, and any mixer notice that changes with the mixer. Now exactly one
 * region speaks, visually hidden, and says only the change, plainly:
 * "KNX offline", "KNX and DMX offline. Mixer degraded.", "KNX back online".
 *
 * - Changes inside `COALESCE_MS` become one sentence; the sentence is built at
 *   the end of the window from what changed since the last one, so a device
 *   that drops and returns inside it is never mentioned.
 * - An unchanged state is never repeated: a device retrying while offline
 *   (offline -> connecting -> offline) stays silent, and "connecting" is never
 *   spoken. A device that becomes not configured is silent.
 * - The text is cleared after `CLEAR_MS` so a screen reader's browse cursor
 *   never finds a stale "KNX offline" — and the next message is a change.
 *
 * Companion changes: the device list and the summary LED lost their
 * `aria-live` (they keep their accessible names), and the device-offline
 * banners are not live regions (`SystemBanners`) — the announcer speaks them.
 * Banners for other conditions stay live.
 */
import { useEffect, useRef, useState } from "react";

import { DEVICE_LABELS, DEVICE_ORDER, useDeviceStatuses, type DeviceName, type DeviceStatus } from "@/live/deviceStatus";

const COALESCE_MS = 1000;
const CLEAR_MS = 10_000;

type Reported = "ok" | "offline" | "degraded";

function bucket(status: DeviceStatus): Reported | null {
  switch (status) {
    case "error":
      return "offline";
    case "degraded":
      return "degraded";
    case "connected":
    case "unconfigured":
      return "ok";
    case "connecting":
      return null; // transient: neither a new problem nor a recovery
  }
}

function names(list: readonly DeviceName[]): string {
  const labels = list.map((name) => DEVICE_LABELS[name]);
  if (labels.length <= 1) return labels.join("");
  return `${labels.slice(0, -1).join(", ")} and ${labels[labels.length - 1]}`;
}

/** The sentence for what changed since `reported`, updating it; "" when nothing is worth saying. */
function describeChanges(
  reported: Record<DeviceName, Reported>,
  statuses: Readonly<Record<DeviceName, DeviceStatus>>,
): string {
  const offline: DeviceName[] = [];
  const degraded: DeviceName[] = [];
  const recovered: DeviceName[] = [];
  for (const name of DEVICE_ORDER) {
    const now = bucket(statuses[name]);
    if (now === null || now === reported[name]) continue;
    if (now === "offline") offline.push(name);
    else if (now === "degraded") degraded.push(name);
    else if (statuses[name] === "connected") recovered.push(name);
    reported[name] = now;
  }
  const parts: string[] = [];
  if (offline.length > 0) parts.push(`${names(offline)} offline`);
  if (degraded.length > 0) parts.push(`${names(degraded)} degraded`);
  if (recovered.length > 0) parts.push(`${names(recovered)} back online`);
  return parts.join(". ");
}

export function ConnectionAnnouncer() {
  const statuses = useDeviceStatuses();
  const [message, setMessage] = useState("");
  const reported = useRef<Record<DeviceName, Reported>>({ knx: "ok", dmx: "ok", mixer: "ok", projector: "ok", hdmi: "ok" });
  const latest = useRef(statuses);
  const flushTimer = useRef<number | undefined>(undefined);
  const clearTimer = useRef<number | undefined>(undefined);

  // A primitive key, so this re-runs on a status change and not on every render.
  const key = DEVICE_ORDER.map((name) => statuses[name]).join();
  useEffect(() => {
    latest.current = statuses;
    if (flushTimer.current !== undefined) return;
    flushTimer.current = window.setTimeout(() => {
      flushTimer.current = undefined;
      const text = describeChanges(reported.current, latest.current);
      if (text === "") return;
      setMessage(text);
      window.clearTimeout(clearTimer.current);
      clearTimer.current = window.setTimeout(() => setMessage(""), CLEAR_MS);
    }, COALESCE_MS);
    // `statuses` is read through `latest`; `key` is the change signal.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  useEffect(
    () => () => {
      window.clearTimeout(flushTimer.current);
      flushTimer.current = undefined;
      window.clearTimeout(clearTimer.current);
    },
    [],
  );

  return (
    <div className="sr-only" role="status" aria-live="polite" aria-atomic="true" data-testid="connection-announcer">
      {message}
    </div>
  );
}
