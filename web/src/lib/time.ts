/* Time formatting for the status bar (spec §21.7). Pacific/Auckland is the appliance's zone (§4.9). */

const pad2 = (n: number): string => String(n).padStart(2, "0");

/** Appliance clock to the minute: "19:42". */
export function formatClock(date: Date): string {
  return `${pad2(date.getHours())}:${pad2(date.getMinutes())}`;
}

/** Elapsed time in H:MM:SS, monospaced by the caller so digits do not shift. */
export function formatElapsed(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return `${h}:${pad2(m)}:${pad2(s)}`;
}

/** Rate-limit countdown: "12:34" for minutes:seconds. */
export function formatCountdown(seconds: number): string {
  const total = Math.max(0, Math.ceil(seconds));
  return `${Math.floor(total / 60)}:${pad2(total % 60)}`;
}

/** "Just now", "45 s ago", "3 min ago", "2 h ago", or the date. */
export function formatRelative(iso: string | null | undefined, now: number = Date.now()): string {
  if (!iso) return "—";
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return "—";
  const seconds = Math.max(0, Math.round((now - then) / 1000));
  if (seconds < 10) return "Just now";
  if (seconds < 60) return `${seconds} s ago`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} h ago`;
  return new Date(then).toLocaleDateString("en-NZ");
}

/** The clock carries an amber marker when the two clocks differ by more than a minute (§21.7). */
export const CLOCK_SKEW_THRESHOLD_MS = 60_000;

export function clocksDisagree(serverTimeOffsetMs: number): boolean {
  return Math.abs(serverTimeOffsetMs) > CLOCK_SKEW_THRESHOLD_MS;
}
