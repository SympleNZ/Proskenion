/*
 * Time formatting for the whole web app (spec §21.7, §4.9). Every formatter
 * here fixes the appliance's own zone, Pacific/Auckland, and locale, en-NZ —
 * never the browser's. That is deliberate: §21.7's status bar clock is "the
 * appliance's time, not the browser's" because that is the clock scheduled
 * rules fire against (§8.3), and the same reasoning extends to every other
 * timestamp the interface shows — a viewer on a laptop in another time zone
 * must see the same date and time a viewer standing at the appliance would.
 *
 * This is the *only* module that is allowed to call `toLocaleDateString`,
 * `toLocaleTimeString`, `toLocaleString`, or read a `Date`'s local
 * components (`getHours`, `getDate`, `setHours`, and friends) — those all
 * follow the browser's own zone, which is exactly the bug this module
 * exists to prevent. An ESLint rule enforces this outside this file
 * (eslint.config.js).
 */

const TIME_ZONE = "Pacific/Auckland";
const LOCALE = "en-NZ";

const pad2 = (n: number): string => String(n).padStart(2, "0");

function toEpochMs(at: string | Date): number {
  return at instanceof Date ? at.getTime() : Date.parse(at);
}

/** "8 April 2026" — the long date form used across Admin (§21.23, §21.24). */
export function formatDate(at: string | Date): string {
  const ms = toEpochMs(at);
  if (Number.isNaN(ms)) return typeof at === "string" ? at : "";
  return new Intl.DateTimeFormat(LOCALE, { day: "numeric", month: "long", year: "numeric", timeZone: TIME_ZONE }).format(ms);
}

/** "8 Apr" — the short date form, for a deadline or a history row that might not be today. */
export function formatDateShort(at: string | Date): string {
  const ms = toEpochMs(at);
  if (Number.isNaN(ms)) return typeof at === "string" ? at : "";
  return new Intl.DateTimeFormat(LOCALE, { day: "numeric", month: "short", timeZone: TIME_ZONE }).format(ms);
}

/** "19:42", or "19:42:07" with `seconds` — 24-hour. Accepts an ISO string or a `Date` (e.g. the
 * appliance clock in `components/statusbar/Clock.tsx`, computed from `now + serverTimeOffset`). */
export function formatTime(at: string | Date, opts: { seconds?: boolean } = {}): string {
  const ms = toEpochMs(at);
  if (Number.isNaN(ms)) return typeof at === "string" ? at : "";
  return new Intl.DateTimeFormat(LOCALE, {
    hour: "2-digit",
    minute: "2-digit",
    ...(opts.seconds ? { second: "2-digit" } : {}),
    hour12: false,
    timeZone: TIME_ZONE,
  }).format(ms);
}

/** Date and 24-hour clock together: "8 Apr, 19:42". */
export function formatDateTime(at: string | Date): string {
  const ms = toEpochMs(at);
  if (Number.isNaN(ms)) return typeof at === "string" ? at : "";
  const date = new Date(ms);
  return `${formatDateShort(date)}, ${formatTime(date)}`;
}

/** The Pacific/Auckland calendar date, as "YYYY-MM-DD" — for day-boundary comparisons that must
 * not depend on the viewer's own zone (a UTC laptop crosses midnight hours away from Auckland). */
function aucklandDateKey(ms: number): string {
  return new Intl.DateTimeFormat("en-CA", { timeZone: TIME_ZONE, year: "numeric", month: "2-digit", day: "2-digit" }).format(ms);
}

/** Whole Pacific/Auckland calendar days between `atMs` and `nowMs` (today is 0), never negative. */
export function daysSince(atMs: number, nowMs: number = Date.now()): number {
  const at = Date.parse(`${aucklandDateKey(atMs)}T00:00:00Z`);
  const now = Date.parse(`${aucklandDateKey(nowMs)}T00:00:00Z`);
  return Math.max(0, Math.round((now - at) / 86_400_000));
}

/** Appliance clock to the minute: "19:42" (§21.7). Always Pacific/Auckland, whatever zone the
 * viewing device is in — the point of showing it is that it is *not* the browser's own idea of
 * the time. Also used for the detail sheet's "This device" row: both rows format the same way,
 * so a real difference between the two clocks shows even when the viewer is in another zone. */
export function formatClock(date: Date): string {
  return formatTime(date);
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
  return formatDate(new Date(then));
}

/** The clock carries an amber marker when the two clocks differ by more than a minute (§21.7). */
export const CLOCK_SKEW_THRESHOLD_MS = 60_000;

export function clocksDisagree(serverTimeOffsetMs: number): boolean {
  return Math.abs(serverTimeOffsetMs) > CLOCK_SKEW_THRESHOLD_MS;
}
