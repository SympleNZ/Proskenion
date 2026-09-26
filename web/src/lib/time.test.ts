/*
 * Time formatting (spec §21.7, §4.9). Every formatter here must render
 * Pacific/Auckland regardless of the runtime's own zone, so this suite runs
 * under UTC (`vite.config.ts`'s `test.env.TZ`) and starts by proving that —
 * a regression there would let every other assertion below pass for the
 * wrong reason, against a runtime that happened to already be Auckland.
 */
import { describe, expect, it } from "vitest";

import {
  clocksDisagree,
  daysSince,
  formatClock,
  formatDate,
  formatDateShort,
  formatDateTime,
  formatRelative,
  formatTime,
} from "./time";

describe("the test runtime", () => {
  it("runs in UTC, so a formatter that silently used the runtime's own zone would be caught here as it is on CI", () => {
    expect(new Date().getTimezoneOffset()).toBe(0);
  });
});

describe("formatDate", () => {
  it("renders the long form in Pacific/Auckland, twelve hours ahead of UTC in southern winter", () => {
    // 09:00 NZST (+12) on 8 April is 21:00 UTC the day before — the exact
    // case the CI-vs-NZ-machine bug turned on (UsersScreen.test.tsx).
    expect(formatDate("2026-04-08T09:00:00+12:00")).toBe("8 April 2026");
    expect(formatDate("2026-04-07T21:00:00Z")).toBe("8 April 2026");
  });

  it("renders the long form in Pacific/Auckland, thirteen hours ahead of UTC in southern summer", () => {
    expect(formatDate("2026-01-15T11:00:00Z")).toBe("16 January 2026");
  });

  it("returns the input unchanged when it cannot be parsed", () => {
    expect(formatDate("not-a-date")).toBe("not-a-date");
  });
});

describe("formatDateShort", () => {
  it("renders day and short month only, in Pacific/Auckland", () => {
    expect(formatDateShort("2026-04-07T21:00:00Z")).toBe("8 Apr");
  });
});

describe("formatTime", () => {
  it("renders 24-hour Pacific/Auckland time from an ISO string", () => {
    expect(formatTime("2026-04-07T21:05:00Z")).toBe("09:05");
  });

  it("renders from a Date, matching the appliance clock's own input", () => {
    expect(formatTime(new Date("2026-04-07T21:05:00Z"))).toBe("09:05");
  });

  it("adds seconds only when asked", () => {
    expect(formatTime("2026-04-07T21:05:09Z", { seconds: true })).toBe("09:05:09");
  });
});

describe("formatClock", () => {
  it("is the appliance's time, not the browser's (§21.7) — Pacific/Auckland whatever the runtime's own zone", () => {
    expect(formatClock(new Date("2026-04-07T21:05:00Z"))).toBe("09:05");
  });
});

describe("formatDateTime", () => {
  it("joins the short date and the 24-hour clock", () => {
    expect(formatDateTime("2026-04-07T21:05:00Z")).toBe("8 Apr, 09:05");
  });
});

describe("formatRelative", () => {
  it("falls back to the long Pacific/Auckland date beyond a day, not the browser's zone", () => {
    const then = Date.parse("2026-04-07T21:00:00Z"); // 8 April, NZ
    const now = then + 25 * 60 * 60 * 1000; // 25 h later
    expect(formatRelative("2026-04-07T21:00:00Z", now)).toBe("8 April 2026");
  });
});

describe("daysSince", () => {
  it("counts whole Pacific/Auckland calendar days, not UTC ones", () => {
    // 23:30 UTC on 7 April is already 8 April in Auckland (NZST, +12); from
    // a "now" that is 30 minutes later in UTC but the same Auckland day,
    // this must read 0, not 1 — the same day-boundary bug `formatWhen`
    // (admin/backup/format.ts) had before it moved onto this helper.
    const at = Date.parse("2026-04-07T23:30:00Z");
    const now = Date.parse("2026-04-07T23:45:00Z");
    expect(daysSince(at, now)).toBe(0);
  });

  it("counts 1 the Auckland calendar day after", () => {
    const at = Date.parse("2026-04-07T09:00:00Z"); // 21:00 NZ, 7 April
    const now = Date.parse("2026-04-08T09:00:00Z"); // 21:00 NZ, 8 April
    expect(daysSince(at, now)).toBe(1);
  });
});

describe("clocksDisagree", () => {
  it("is unaffected by zone — it compares raw offsets, not formatted times", () => {
    expect(clocksDisagree(0)).toBe(false);
    expect(clocksDisagree(59_000)).toBe(false);
    expect(clocksDisagree(61_000)).toBe(true);
    expect(clocksDisagree(-61_000)).toBe(true);
  });
});
