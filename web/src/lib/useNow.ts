/*
 * A shared once-a-second clock as an external store, so components that show
 * elapsed or wall time read a snapshot rather than calling Date.now() during
 * render. One interval serves every subscriber and stops when none remain.
 */
import { useSyncExternalStore } from "react";

let now = Date.now();
let timer: ReturnType<typeof setInterval> | null = null;
const listeners = new Set<() => void>();

function subscribe(cb: () => void): () => void {
  listeners.add(cb);
  if (timer === null) {
    now = Date.now();
    timer = setInterval(() => {
      now = Date.now();
      listeners.forEach((l) => l());
    }, 1000);
  }
  return () => {
    listeners.delete(cb);
    if (listeners.size === 0 && timer !== null) {
      clearInterval(timer);
      timer = null;
    }
  };
}

const getNow = () => now;

/** Epoch ms, refreshed every second while anything is mounted that uses it. */
export function useNow(): number {
  return useSyncExternalStore(subscribe, getNow, getNow);
}
