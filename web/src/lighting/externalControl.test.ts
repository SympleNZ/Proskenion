/*
 * Read-only under external control (spec §7.2.3's behaviour matrix, §7.2.7,
 * §9.4, §9.5, §21.11): only the DMX pass is suspended; the KNX pass — house
 * lighting — runs normally throughout, so only a `dmx` channel goes
 * read-only, and with it the master and any group holding a DMX fixture.
 */
import { describe, expect, it } from "vitest";

import type { ExternalControl } from "@/live/store";

import {
  isGroupReadOnlyUnderExternalControl,
  isMasterReadOnlyUnderExternalControl,
  isReadOnlyUnderExternalControl,
  setLevelTargetsUnderExternalControl,
} from "./externalControl";

const ACTIVE: readonly ExternalControl[] = ["detected", "manual"];
const DMX = { type: "dmx" as const };
const KNX = { type: "knx_dimmer" as const };

describe("isReadOnlyUnderExternalControl", () => {
  it("a dmx channel is interactive off, and read-only once external control is active", () => {
    const channel = { type: "dmx" as const };
    expect(isReadOnlyUnderExternalControl(channel, "off")).toBe(false);
    expect(isReadOnlyUnderExternalControl(channel, "detected")).toBe(true);
    expect(isReadOnlyUnderExternalControl(channel, "manual")).toBe(true);
  });

  it("a knx_dimmer channel stays interactive throughout — house lighting is never gated by DMX state (§7.2.3)", () => {
    const channel = { type: "knx_dimmer" as const };
    expect(isReadOnlyUnderExternalControl(channel, "off")).toBe(false);
    expect(isReadOnlyUnderExternalControl(channel, "detected")).toBe(false);
    expect(isReadOnlyUnderExternalControl(channel, "manual")).toBe(false);
  });
});

describe("isMasterReadOnlyUnderExternalControl", () => {
  it("is interactive off, and read-only once external control is active — it scales DMX output only (§9.5)", () => {
    expect(isMasterReadOnlyUnderExternalControl("off")).toBe(false);
    for (const state of ACTIVE) expect(isMasterReadOnlyUnderExternalControl(state)).toBe(true);
  });
});

describe("isGroupReadOnlyUnderExternalControl", () => {
  it("every group is interactive while external control is off", () => {
    for (const members of [[DMX], [KNX], [DMX, KNX], [], undefined]) {
      expect(isGroupReadOnlyUnderExternalControl(members, "off")).toBe(false);
    }
  });

  it("a group holding a DMX fixture is read-only under external control, KNX members or not", () => {
    for (const state of ACTIVE) {
      expect(isGroupReadOnlyUnderExternalControl([DMX], state)).toBe(true);
      expect(isGroupReadOnlyUnderExternalControl([KNX, DMX], state)).toBe(true);
    }
  });

  it("a group of KNX dimmers only is unaffected by external control, and so is an empty group", () => {
    for (const state of ACTIVE) {
      expect(isGroupReadOnlyUnderExternalControl([KNX, KNX], state)).toBe(false);
      expect(isGroupReadOnlyUnderExternalControl([], state)).toBe(false);
    }
  });

  it("a group whose membership has not loaded is treated as stage lighting", () => {
    for (const state of ACTIVE) expect(isGroupReadOnlyUnderExternalControl(undefined, state)).toBe(true);
  });
});

describe("setLevelTargetsUnderExternalControl (§21.11, §21.12)", () => {
  const wash = { id: 1, type: "dmx" as const };
  const special = { id: 2, type: "dmx" as const };
  const house = { id: 3, type: "knx_dimmer" as const };

  it("sets the whole selection while external control is off", () => {
    expect(setLevelTargetsUnderExternalControl([wash, house], "off")).toEqual({ settable: [wash, house], skipped: [] });
  });

  it("a selection of stage fixtures has nothing to set under external control", () => {
    for (const state of ACTIVE) {
      expect(setLevelTargetsUnderExternalControl([wash, special], state)).toEqual({ settable: [], skipped: [wash, special] });
    }
  });

  it("a selection of house dimmers only is set as it is with external control off", () => {
    for (const state of ACTIVE) {
      expect(setLevelTargetsUnderExternalControl([house], state)).toEqual({ settable: [house], skipped: [] });
    }
  });

  it("a mixed selection sets its house dimmers and skips its stage fixtures", () => {
    for (const state of ACTIVE) {
      expect(setLevelTargetsUnderExternalControl([wash, house, special], state)).toEqual({
        settable: [house],
        skipped: [wash, special],
      });
    }
  });
});
