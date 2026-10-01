/*
 * The client half of a group's BUMP (owner decision 2026-10-01, "Option A"):
 * flash while held. Pressing sends `lighting_bump` 1, the press is re-sent
 * every BUMP_REFRESH_MS while it stays held, and letting go sends 0. The
 * server flashes the group's DMX members to full × the master while any
 * connection holds it, and lets a hold go by itself when the refreshes stop
 * (1.5 s, `proskenion/core/dmx/bump.py`), when the socket closes, and when
 * this page reports going to the background — so a lost release frame or a
 * tablet that dropped off the Wi-Fi with a finger down can never leave the
 * stage at full.
 *
 * A BUMP writes nothing to the live store: no level moves and no fader
 * follows it. What this module keeps is only which on-screen buttons are
 * holding which group, so a button can light while it is held and two
 * buttons on one group (unusual, but a phone and a page can both show it)
 * release the group only when both have let go.
 *
 * Nothing is replayed across a reconnection (§10.7): when the refresh finds
 * the socket gone, every local hold is dropped and its button goes dark. A
 * finger still down after the socket comes back does not re-flash the stage.
 */
import { sendBump } from "@/live/socket";

/** How often a held BUMP is re-sent; the server's timeout is three of these. */
export const BUMP_REFRESH_MS = 500;

type Listener = () => void;

/** group id → the buttons holding it. */
const holds = new Map<number, Set<string>>();
const listeners = new Set<Listener>();
let refreshTimer: ReturnType<typeof setInterval> | null = null;

function notify(): void {
  for (const listener of [...listeners]) listener();
}

export function subscribeBumps(listener: Listener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** Whether the button `holder` is holding a BUMP down. */
export function isHolding(holder: string): boolean {
  for (const holders of holds.values()) if (holders.has(holder)) return true;
  return false;
}

/** Whether any button is holding `groupId`'s BUMP down. */
export function isGroupBumped(groupId: number): boolean {
  return holds.has(groupId);
}

function startRefresh(): void {
  if (refreshTimer !== null) return;
  refreshTimer = setInterval(refresh, BUMP_REFRESH_MS);
}

function stopRefresh(): void {
  if (refreshTimer === null) return;
  clearInterval(refreshTimer);
  refreshTimer = null;
}

function refresh(): void {
  for (const groupId of holds.keys()) {
    if (!sendBump(groupId, true)) {
      // The socket is gone, and with it the server's hold: drop every local
      // one so no button stays lit, and nothing re-presses on reconnection.
      dropAll();
      return;
    }
  }
}

function dropAll(): void {
  if (holds.size === 0) return;
  holds.clear();
  stopRefresh();
  notify();
}

/** The button `holder` pressed `groupId`'s BUMP. Returns whether the press was sent. */
export function holdBump(groupId: number, holder: string): boolean {
  const holders = holds.get(groupId);
  if (holders) {
    if (!holders.has(holder)) {
      holders.add(holder);
      notify();
    }
    return true;
  }
  if (!sendBump(groupId, true)) return false;
  holds.set(groupId, new Set([holder]));
  startRefresh();
  notify();
  return true;
}

/** The button `holder` let go. The group is released when nobody holds it. */
export function releaseBump(groupId: number, holder: string): void {
  const holders = holds.get(groupId);
  if (!holders?.delete(holder)) return;
  if (holders.size === 0) {
    holds.delete(groupId);
    sendBump(groupId, false);
    if (holds.size === 0) stopRefresh();
  }
  notify();
}

/** Tests only: forget every hold without sending anything. */
export function resetBumps(): void {
  holds.clear();
  stopRefresh();
  notify();
}
