/*
 * Shared timer (spec §21.7, §16, §16.8). A thin view over the live store,
 * and the three routes that change it.
 *
 * The timer lives on the server and broadcasts like any other state. The
 * `timer` message carries `started_at` (ISO 8601 with offset, §4.9) and
 * `accumulated_ms` — never a running elapsed count — so the client computes
 * elapsed itself, against the appliance's clock (`now` plus the session's
 * server time offset), and a tab that was asleep for ten minutes shows the
 * right number the moment it wakes.
 *
 * Start, stop and reset are `POST /timer/{action}` (§16, admin and operator
 * only). Nothing here writes the store: the store is fed only by `timer`
 * frames, so every client — including the one that pressed the button —
 * shows the server's state and nothing else. A press is not a gesture with a
 * value to hold in §21.2's pending overlay; the button is simply busy until
 * the request settles, and the frame that follows is what moves the display.
 */
import { useMutation, type UseMutationResult } from "@tanstack/react-query";
import { useSyncExternalStore } from "react";

import { api } from "@/api/client";
import { getTimerState, subscribeTimer, type TimerState } from "@/live/store";

export type { TimerState } from "@/live/store";
export { getTimerState, subscribeTimer } from "@/live/store";

export function elapsedMs(timer: TimerState, now: number = Date.now()): number {
  const running = timer.running && timer.startedAt !== null ? Math.max(0, now - timer.startedAt) : 0;
  return timer.accumulated + running;
}

export type TimerAction = "start" | "stop" | "reset";

/** `POST /timer/{action}`'s answer: the `timer` frame's own fields. */
export interface TimerStateResponse {
  running: boolean;
  started_at: string | null;
  accumulated_ms: number;
}

export function postTimerAction(action: TimerAction): Promise<TimerStateResponse> {
  return api<TimerStateResponse>(`/timer/${action}`, { method: "POST" });
}

/** The mutation behind the status bar's timer buttons. Its answer is not written to the store: the frame is. */
export function useTimerAction(): UseMutationResult<TimerStateResponse, unknown, TimerAction> {
  return useMutation({ mutationFn: postTimerAction });
}

export function useTimer(): TimerState {
  return useSyncExternalStore(subscribeTimer, getTimerState, getTimerState);
}
