/*
 * The live telegram monitor's transport (spec §21.19 *Live monitor*, §7.1).
 *
 * `GET /knx/monitor` is server-sent events, not the WebSocket the rest of the
 * live state uses — `proskenion/api/knx.py`'s docstring is explicit that this
 * is a plain SSE stream, replaying a short backlog before live entries. This
 * is deliberately its own small connection, not routed through `live/socket.ts`:
 * that socket is the always-on connection for the signed-in session (§21.2);
 * this one exists only while the monitor panel is actually visible, and
 * closing it the moment it is not is the point (a busy KNX bus must not run a
 * hidden SSE connection collecting telegrams nobody is looking at).
 *
 * State lives in a small external store (`MonitorStream`), read with
 * `useSyncExternalStore` — the same shape CONVENTIONS "Interface" mandates
 * for live state generally (§21.2, B32) — rather than `useState` updated from
 * inside an effect: the effect that opens and closes the connection then only
 * calls plain methods on the stream, never a React state setter directly, so
 * it stays a pure "synchronise with an external system" effect.
 */
import { useEffect, useState, useSyncExternalStore } from "react";

import type { MonitorEntry } from "./types";

/** The parts of `EventSource` this module uses; a fake one in tests supplies the same. */
export interface EventSourceLike {
  close(): void;
  onmessage: ((event: { data: string }) => void) | null;
  onerror: ((event: unknown) => void) | null;
}

export type CreateEventSource = (url: string) => EventSourceLike;

export const MONITOR_URL = "/api/v1/knx/monitor";

/** A busy TP1 bus can carry tens of telegrams a second (§7.1); keeping only
 * the most recent of these is what "a bounded list so a busy bus cannot grow
 * memory without limit" (task scope) means in practice. */
export const MAX_MONITOR_ENTRIES = 200;

function defaultCreateEventSource(url: string): EventSourceLike {
  return new EventSource(url) as unknown as EventSourceLike;
}

interface Snapshot {
  /** Newest first. */
  entries: MonitorEntry[];
  connected: boolean;
  paused: boolean;
}

const EMPTY_SNAPSHOT: Snapshot = { entries: [], connected: false, paused: false };

/**
 * One SSE connection's state, outside React. `connect`/`disconnect` are
 * plain methods an effect calls to synchronise with the external system;
 * `subscribe`/`getSnapshot` are what `useSyncExternalStore` reads — nothing
 * here calls a React state setter.
 */
class MonitorStream {
  private readonly createSource: CreateEventSource;
  private readonly maxEntries: number;
  private source: EventSourceLike | null = null;
  private readonly listeners = new Set<() => void>();
  private snapshot: Snapshot = EMPTY_SNAPSHOT;

  constructor(createSource: CreateEventSource, maxEntries: number) {
    this.createSource = createSource;
    this.maxEntries = maxEntries;
  }

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };

  getSnapshot = (): Snapshot => this.snapshot;

  private publish(patch: Partial<Snapshot>): void {
    this.snapshot = { ...this.snapshot, ...patch };
    for (const listener of this.listeners) listener();
  }

  connect(): void {
    if (this.source) return;
    const source = this.createSource(MONITOR_URL);
    this.source = source;
    source.onmessage = (event) => {
      if (this.snapshot.paused) return;
      let parsed: MonitorEntry;
      try {
        parsed = JSON.parse(event.data) as MonitorEntry;
      } catch {
        return; // a malformed frame is dropped, never crashes the panel
      }
      const next = [parsed, ...this.snapshot.entries];
      this.publish({ entries: next.length > this.maxEntries ? next.slice(0, this.maxEntries) : next });
    };
    source.onerror = () => this.publish({ connected: false });
    this.publish({ connected: true });
  }

  disconnect(): void {
    const source = this.source;
    this.source = null;
    if (source) {
      source.onmessage = null;
      source.onerror = null;
      source.close();
    }
    if (this.snapshot.connected) this.publish({ connected: false });
  }

  pause(): void {
    this.publish({ paused: true });
  }

  resume(): void {
    this.publish({ paused: false });
  }

  clear(): void {
    this.publish({ entries: [] });
  }
}

export interface UseKnxMonitorOptions {
  createSource?: CreateEventSource | undefined;
  maxEntries?: number | undefined;
}

export interface KnxMonitorState {
  /** Newest first. */
  entries: MonitorEntry[];
  connected: boolean;
  paused: boolean;
  pause: () => void;
  resume: () => void;
  clear: () => void;
}

/**
 * Opens the SSE connection while `active` is true and closes it the moment it
 * is not — the panel being collapsed, its tab losing visibility, or the
 * component unmounting all set `active` to false through the caller, so there
 * is exactly one place ("this effect's cleanup") a connection can be closed
 * from, which is what keeps switching tabs from leaking one.
 *
 * Pausing stops new telegrams from being appended without closing the
 * connection — resuming does not need to replay anything, because the
 * backlog `knx.monitor.stream()` sends on connect would only repeat what was
 * already shown before the pause.
 */
export function useKnxMonitor(active: boolean, options: UseKnxMonitorOptions = {}): KnxMonitorState {
  const { createSource = defaultCreateEventSource, maxEntries = MAX_MONITOR_ENTRIES } = options;

  // State, not a ref: the instance is only ever constructed once (the
  // initialiser function runs on mount alone), but it still needs to be read
  // safely during render — to pass to `useSyncExternalStore` and the effect
  // below — and a `useState` value, unlike a ref's `.current`, is exactly
  // that: an ordinary render-time value.
  const [stream] = useState(() => new MonitorStream(createSource, maxEntries));

  useEffect(() => {
    if (active) stream.connect();
    else stream.disconnect();
    return () => stream.disconnect();
  }, [active, stream]);

  const snapshot = useSyncExternalStore(stream.subscribe, stream.getSnapshot, stream.getSnapshot);

  return {
    entries: snapshot.entries,
    connected: snapshot.connected,
    paused: snapshot.paused,
    pause: stream.pause.bind(stream),
    resume: stream.resume.bind(stream),
    clear: stream.clear.bind(stream),
  };
}
