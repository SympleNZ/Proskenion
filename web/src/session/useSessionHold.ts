/*
 * The visibility rule (spec §6.4, B65): foreground presence holds a session,
 * not traffic. While the page is visible, GET /auth/session every five
 * minutes re-issues the cookie; when hidden, nothing is sent and the server's
 * idle timer runs. Pings, WebSocket frames and broadcasts never count — they
 * go nowhere near this hook.
 */
import { useEffect } from "react";

export const SESSION_HOLD_INTERVAL_MS = 5 * 60_000;

export function isPageVisible(): boolean {
  return typeof document === "undefined" || document.visibilityState === "visible";
}

export function useSessionHold(
  enabled: boolean,
  refresh: () => Promise<unknown>,
  intervalMs: number = SESSION_HOLD_INTERVAL_MS,
): void {
  useEffect(() => {
    if (!enabled) return undefined;

    let timer: ReturnType<typeof setInterval> | null = null;
    const tick = () => {
      void refresh().catch(() => undefined);
    };
    const start = () => {
      if (timer !== null) return;
      timer = setInterval(tick, intervalMs);
    };
    const stop = () => {
      if (timer === null) return;
      clearInterval(timer);
      timer = null;
    };
    const onVisibility = () => {
      if (isPageVisible()) {
        // Coming back from hidden: hold immediately, then every interval.
        tick();
        start();
      } else {
        stop();
      }
    };

    if (isPageVisible()) start();
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      stop();
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [enabled, refresh, intervalMs]);
}
