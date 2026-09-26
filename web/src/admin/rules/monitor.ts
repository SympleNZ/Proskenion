/*
 * The derived-status live monitor (spec §8.10, §21.17): server-sent events,
 * in the same shape as the KNX telegram monitor (§21.19) — every status as
 * it stands, replayed first, then each change of value as it happens.
 *
 * Connects only while `enabled` is true, and closes the instant it goes
 * false or the component unmounts, so switching from the Derived status tab
 * to the Rules tab — or leaving the page — never leaves a stream open. The
 * caller decides what "enabled" means; `DerivedStatusTab` ties it to both
 * the in-page tab being the visible one and the document itself being
 * visible (§6.4's spirit: presence, not an open connection nobody is
 * looking at, is what should hold a stream open).
 *
 * Reconnection: §8.10 and §21.17 do not specify backoff for this stream, and
 * there is no existing frontend for the KNX monitor in this codebase to
 * match yet either. The browser's own `EventSource` already retries a
 * dropped connection on its own schedule (or the server's, via the `retry:`
 * field) without any code here, so nothing is layered on top of that beyond
 * reporting the transient state as `"error"` — simple, and enough until a
 * real reconnect problem is observed in commissioning.
 */
import { useEffect, useReducer } from "react";

import { API_BASE } from "@/api/client";

import type { StatusReading } from "./types";

export type MonitorConnection = "idle" | "connecting" | "open" | "error";

export interface MonitorState {
  connection: MonitorConnection;
  /** Keyed by derived-status id, the same key `GET /derived-status/state` uses. */
  readings: ReadonlyMap<number, StatusReading>;
}

type Action =
  | { type: "connecting" }
  | { type: "open" }
  | { type: "error" }
  | { type: "idle" }
  | { type: "reading"; reading: StatusReading };

function reduce(state: MonitorState, action: Action): MonitorState {
  switch (action.type) {
    case "connecting":
      return { ...state, connection: "connecting" };
    case "open":
      return { ...state, connection: "open" };
    case "error":
      return { ...state, connection: "error" };
    case "idle":
      return { connection: "idle", readings: state.readings };
    case "reading": {
      const readings = new Map(state.readings);
      readings.set(action.reading.id, action.reading);
      return { ...state, readings };
    }
  }
}

const INITIAL: MonitorState = { connection: "idle", readings: new Map() };

export function useDerivedStatusMonitor(enabled: boolean): MonitorState {
  const [state, dispatch] = useReducer(reduce, INITIAL);

  useEffect(() => {
    if (!enabled) {
      dispatch({ type: "idle" });
      return undefined;
    }
    dispatch({ type: "connecting" });
    const source = new EventSource(`${API_BASE}/derived-status/monitor`, { withCredentials: true });
    const onOpen = () => dispatch({ type: "open" });
    const onError = () => dispatch({ type: "error" });
    const onStatus = (event: MessageEvent<string>) => {
      try {
        const reading = JSON.parse(event.data) as StatusReading;
        dispatch({ type: "reading", reading });
      } catch {
        // A malformed frame is dropped; the stream continues.
      }
    };
    source.addEventListener("open", onOpen);
    source.addEventListener("error", onError);
    source.addEventListener("status", onStatus);
    return () => {
      source.removeEventListener("open", onOpen);
      source.removeEventListener("error", onError);
      source.removeEventListener("status", onStatus);
      source.close();
      dispatch({ type: "idle" });
    };
  }, [enabled]);

  return state;
}
