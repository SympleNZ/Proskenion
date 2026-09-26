/*
 * Formatting (spec §21.24 *Health*, §5.4). The distinction that matters: a
 * reading of zero is a reading, and a metric that could not be read is not.
 */
import { describe, expect, it } from "vitest";

import { formatBytes, formatCount, formatPercent, formatTemperature, formatUptime, formatUsage, NOT_AVAILABLE } from "./format";

describe("health formatting", () => {
  it("keeps zero distinguishable from unavailable", () => {
    expect(formatCount(0)).toBe("0");
    expect(formatCount(null)).toBe(NOT_AVAILABLE);
    expect(formatTemperature(0)).toBe("0 °C");
    expect(formatTemperature(null)).toBe(NOT_AVAILABLE);
    expect(formatPercent(0)).toBe("0 %");
    expect(formatPercent(null)).toBe(NOT_AVAILABLE);
    expect(formatBytes(0)).toBe("0 bytes");
    expect(formatBytes(null)).toBe(NOT_AVAILABLE);
  });

  it("scales bytes without false precision", () => {
    expect(formatBytes(4_200_000_000)).toBe("4.2 GB");
    expect(formatBytes(16_000_000_000)).toBe("16 GB");
    expect(formatBytes(12_000_000)).toBe("12 MB");
    expect(formatUsage(4_200_000_000, 16_000_000_000)).toBe("4.2 GB / 16 GB");
    expect(formatUsage(null, 16_000_000_000)).toBe(NOT_AVAILABLE);
  });

  it("reads uptime in the units that matter at the time", () => {
    expect(formatUptime(42)).toBe("42 s");
    expect(formatUptime(15_132)).toBe("4 h 12 m");
    expect(formatUptime(1_555_200)).toBe("18 d 0 h");
    expect(formatUptime(null)).toBe(NOT_AVAILABLE);
  });
});
