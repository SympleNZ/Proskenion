/*
 * The phone summary LED's worst-state reduction (owner's decision, 2026-09;
 * see `components/statusbar/StatusSummary.tsx`). Pure function, no React —
 * the accessible-name and menu-listing behaviour built on top of it is
 * covered in `components/statusbar/StatusSummary.test.tsx`.
 */
import { describe, expect, it } from "vitest";

import { summariseDeviceStatus } from "./deviceStatus";

describe("summariseDeviceStatus", () => {
  it("is connected when every indicator is connected", () => {
    expect(summariseDeviceStatus(["connected", "connected", "connected"])).toBe("connected");
  });

  it("is degraded when one indicator is degraded and none are worse", () => {
    expect(summariseDeviceStatus(["connected", "degraded", "connected"])).toBe("degraded");
  });

  it("is connecting when one indicator is connecting and none are degraded or worse", () => {
    expect(summariseDeviceStatus(["connected", "connecting", "connected"])).toBe("connecting");
  });

  it("is error when degraded and error are both present — red wins over amber", () => {
    expect(summariseDeviceStatus(["degraded", "error", "connected"])).toBe("error");
  });

  it("is error when connecting and error are both present", () => {
    expect(summariseDeviceStatus(["connecting", "error"])).toBe("error");
  });

  it("ignores unconfigured indicators — they never pull the summary to amber or red", () => {
    expect(summariseDeviceStatus(["connected", "unconfigured", "unconfigured"])).toBe("connected");
    expect(summariseDeviceStatus(["degraded", "unconfigured"])).toBe("degraded");
    expect(summariseDeviceStatus(["error", "unconfigured", "unconfigured"])).toBe("error");
  });

  it("is unconfigured only when every indicator is unconfigured", () => {
    expect(summariseDeviceStatus(["unconfigured", "unconfigured", "unconfigured", "unconfigured", "unconfigured"])).toBe(
      "unconfigured",
    );
  });

  it("is unconfigured on an empty list — nothing to be worried about", () => {
    expect(summariseDeviceStatus([])).toBe("unconfigured");
  });
});
