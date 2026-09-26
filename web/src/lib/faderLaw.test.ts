/*
 * Fader law (spec §5.5, §21.2; acceptance in §22.3). The law is data, so these
 * tests use two different drivers' tables and assert that nothing about the
 * scale, the detents or the conversion is hard-coded.
 */
import { describe, expect, it } from "vitest";

import {
  applyDetent,
  dbToPosition,
  detents,
  DETENT_BREAKOUT_MOVES,
  DETENT_ZONE_DB,
  formatDb,
  formatDbSpoken,
  FREE_GESTURE,
  minorTicks,
  positionToDb,
  scaleTicks,
  type FaderLaw,
} from "./faderLaw";

/** The A&H CQ's law, exactly as §5.5 publishes it. */
const CQ: FaderLaw = [
  { position: 0.0, db: null, label: "-∞" },
  { position: 0.088, db: -50.0 },
  { position: 0.176, db: -40.0, label: "-40" },
  { position: 0.288, db: -30.0, label: "-30" },
  { position: 0.424, db: -20.0, label: "-20" },
  { position: 0.572, db: -10.0, label: "-10" },
  { position: 0.66, db: -5.0, label: "-5" },
  { position: 0.766, db: 0.0, label: "0", detent: true },
  { position: 0.884, db: 5.0, label: "+5" },
  { position: 1.0, db: 10.0, label: "+10" },
];

/** Another desk: a different legend, unity somewhere else, two detents. */
const OTHER_DESK: FaderLaw = [
  { position: 0.0, db: null, label: "-inf" },
  { position: 0.2, db: -30.0, label: "-30" },
  { position: 0.5, db: -12.0, label: "-12", detent: true },
  { position: 0.7, db: 0.0, label: "U", detent: true },
  { position: 1.0, db: 6.0, label: "+6" },
];

/**
 * One display pixel of a fader's travel. The §22.3 tolerance is a pixel, and a
 * fader is a few hundred of them tall, so this is the strictest reading.
 */
const TRAVEL_PIXELS = 1000;
const ONE_PIXEL = 1 / TRAVEL_PIXELS;

describe("fader law round trip", () => {
  it("returns the position it was given, within one display pixel, across the whole travel", () => {
    for (let step = 0; step <= TRAVEL_PIXELS; step += 1) {
      const position = step / TRAVEL_PIXELS;
      const back = dbToPosition(CQ, positionToDb(CQ, position));
      expect(Math.abs(back - position), `position ${position}`).toBeLessThanOrEqual(ONE_PIXEL);
    }
  });

  it("holds at both ends", () => {
    // db: null is the bottom of travel — off, not a number.
    expect(positionToDb(CQ, 0)).toBeNull();
    expect(dbToPosition(CQ, null)).toBe(0);
    expect(positionToDb(CQ, 1)).toBeCloseTo(10, 10);
    expect(dbToPosition(CQ, 10)).toBeCloseTo(1, 10);
  });

  it("holds around unity", () => {
    expect(positionToDb(CQ, 0.766)).toBeCloseTo(0, 10);
    expect(dbToPosition(CQ, 0)).toBeCloseTo(0.766, 10);
    for (const db of [-1.5, -0.5, -0.1, 0, 0.1, 0.5, 1.5]) {
      expect(positionToDb(CQ, dbToPosition(CQ, db)) ?? NaN).toBeCloseTo(db, 8);
    }
  });

  it("round-trips every labelled point exactly", () => {
    for (const point of CQ) {
      if (point.db === null) continue;
      expect(dbToPosition(CQ, point.db), point.label).toBeCloseTo(point.position, 10);
      expect(positionToDb(CQ, point.position) ?? NaN, point.label).toBeCloseTo(point.db, 10);
    }
  });

  it("round-trips another driver's table just as well", () => {
    for (let step = 0; step <= TRAVEL_PIXELS; step += 1) {
      const position = step / TRAVEL_PIXELS;
      const back = dbToPosition(OTHER_DESK, positionToDb(OTHER_DESK, position));
      expect(Math.abs(back - position), `position ${position}`).toBeLessThanOrEqual(ONE_PIXEL);
    }
  });
});

describe("the printed scale comes from the law table", () => {
  it("prints the labelled points of the CQ's law, in travel order", () => {
    expect(scaleTicks(CQ).map((tick) => tick.label)).toEqual(["-∞", "-40", "-30", "-20", "-10", "-5", "0", "+5", "+10"]);
    expect(scaleTicks(CQ).map((tick) => tick.position)).toEqual([
      0.0, 0.176, 0.288, 0.424, 0.572, 0.66, 0.766, 0.884, 1.0,
    ]);
  });

  it("changes with a different driver's table", () => {
    expect(scaleTicks(OTHER_DESK).map((tick) => tick.label)).toEqual(["-inf", "-30", "-12", "U", "+6"]);
    // Unity is somewhere else, and the tick that carries it says so.
    const unity = scaleTicks(OTHER_DESK).find((tick) => tick.db === 0);
    expect(unity?.position).toBe(0.7);
    expect(unity?.label).toBe("U");
    expect(unity?.detent).toBe(true);
  });

  it("renders points with no label as minor ticks with no text", () => {
    expect(minorTicks(CQ)).toEqual([0.088]);
    expect(minorTicks(OTHER_DESK)).toEqual([]);
  });

  it("renders off rather than printing it as a number", () => {
    expect(formatDb(null)).toBe("-∞");
    expect(formatDb(0)).toBe("0.0");
    expect(formatDb(-5)).toBe("-5.0");
    expect(formatDb(3.25)).toBe("+3.3");
  });

  it("speaks the value in words for aria-valuetext, distinct from the printed readout (§24.2)", () => {
    // §24.2's corrected example, verbatim: "−5.0 decibels", "0.0 decibels, unity", "off".
    expect(formatDbSpoken(-5)).toBe("-5.0 decibels");
    expect(formatDbSpoken(3.25)).toBe("+3.3 decibels");
    expect(formatDbSpoken(0, true)).toBe("0.0 decibels, unity");
    expect(formatDbSpoken(null)).toBe("off");
  });
});

describe("detents", () => {
  const unityPosition = 0.766;

  it("declares the driver's detents and no others", () => {
    expect(detents(CQ).map((point) => point.db)).toEqual([0]);
    expect(detents(OTHER_DESK).map((point) => point.db)).toEqual([-12, 0]);
  });

  it("sends the detent's exact dB, not a converted position", () => {
    // A position a hair below unity: the interpolated value is not 0.0.
    const near = unityPosition - 0.005;
    const interpolated = positionToDb(CQ, near) as number;
    expect(interpolated).not.toBe(0);
    expect(Math.abs(interpolated)).toBeLessThan(DETENT_ZONE_DB);

    const move = applyDetent(CQ, near);
    expect(move.held).toBe(true);
    // Exactly 0.0 — what a scene saves and what a hirer ceiling compares against.
    expect(move.db).toBe(0);
    expect(Object.is(move.db, 0)).toBe(true);
    expect(move.detent?.label).toBe("0");
  });

  it("holds through a nudge and releases only on a deliberate push", () => {
    // Enter the zone from below, then push above it.
    let gesture = applyDetent(CQ, unityPosition - 0.005).gesture;
    const beyond = dbToPosition(CQ, DETENT_ZONE_DB + 1);
    for (let move = 1; move < DETENT_BREAKOUT_MOVES; move += 1) {
      const held = applyDetent(CQ, beyond, gesture);
      expect(held.db, `move ${move}`).toBe(0);
      expect(held.held, `move ${move}`).toBe(true);
      gesture = held.gesture;
    }
    const released = applyDetent(CQ, beyond, gesture);
    expect(released.held).toBe(false);
    expect(released.db).toBeCloseTo(DETENT_ZONE_DB + 1, 8);
    expect(released.gesture).toEqual(FREE_GESTURE);
  });

  it("resets the break-out counter when the gesture leaves the zone, from either direction", () => {
    const above = dbToPosition(CQ, DETENT_ZONE_DB + 1);
    const below = dbToPosition(CQ, -(DETENT_ZONE_DB + 1));
    const inside = dbToPosition(CQ, 0.5);

    let gesture = applyDetent(CQ, inside).gesture;
    // Five pushes upward — one short of breaking out.
    for (let move = 1; move < DETENT_BREAKOUT_MOVES; move += 1) gesture = applyDetent(CQ, above, gesture).gesture;
    expect(gesture.breakout).toBe(DETENT_BREAKOUT_MOVES - 1);

    // Back inside: the push starts again.
    const back = applyDetent(CQ, inside, gesture);
    expect(back.held).toBe(true);
    expect(back.gesture.breakout).toBe(0);
    gesture = back.gesture;

    // Five pushes downward now hold just the same, so the sixth is the one
    // that releases — approaching unity from either direction feels identical.
    for (let move = 1; move < DETENT_BREAKOUT_MOVES; move += 1) {
      const held = applyDetent(CQ, below, gesture);
      expect(held.db, `downward move ${move}`).toBe(0);
      gesture = held.gesture;
    }
    const released = applyDetent(CQ, below, gesture);
    expect(released.held).toBe(false);
    expect(released.db).toBeCloseTo(-(DETENT_ZONE_DB + 1), 8);
    // Having broken out, dragging back in snaps again from a clean counter.
    const resnapped = applyDetent(CQ, inside, released.gesture);
    expect(resnapped.held).toBe(true);
    expect(resnapped.db).toBe(0);
  });

  it("snaps to whichever detent the driver declared", () => {
    const move = applyDetent(OTHER_DESK, dbToPosition(OTHER_DESK, -11.5));
    expect(move.held).toBe(true);
    expect(move.db).toBe(-12);
    expect(move.detent?.label).toBe("-12");
  });

  it("never holds a fader that is off", () => {
    const move = applyDetent(CQ, 0);
    expect(move.db).toBeNull();
    expect(move.held).toBe(false);
  });
});
