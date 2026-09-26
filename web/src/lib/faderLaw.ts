/*
 * The fader law, as data (spec §5.5 "The fader law is published as data",
 * §21.2 "Fader law is client-side data"). Pure functions, no React.
 *
 * A fader's travel is not linear in dB and the curve belongs to the driver,
 * not to this application: a generic curve would put the tablet's thumb at a
 * different position from MixPad's for the same level, which reads as a fault
 * rather than a difference. The driver publishes twenty to fifty points and
 * everything — the thumb, the printed scale, the dB readout and the control
 * surface's own faders — interpolates from that one table, so they cannot
 * disagree.
 *
 * dB travels on the wire in both directions (§16.8). The store holds dB;
 * components convert to position only when drawing, which is what keeps the
 * pending overlay and every `set` message in core units — a rejection carrying
 * an authoritative dB needs no conversion to reconcile.
 */

export interface FaderLawPoint {
  /** 0.0 (bottom of travel) to 1.0 (top). */
  position: number;
  /** dB, or null for the bottom of travel: off, not a number (§5.5). */
  db: number | null;
  /** Present on a point the desk prints on its panel; absent points are minor ticks. */
  label?: string;
  /** A value the fader holds at — unity is the obvious case (§5.5). */
  detent?: boolean;
}

export type FaderLaw = readonly FaderLawPoint[];

/** One entry of the printed legend (§5.5 "The table also produces the printed scale"). */
export interface ScaleTick {
  position: number;
  db: number | null;
  label: string;
  detent: boolean;
}

/** The zone either side of a detent's dB value (§5.5). */
export const DETENT_ZONE_DB = 1.5;

/**
 * Pointer moves beyond the zone edge before a detent releases. A counter, not
 * a distance: a distance threshold behaves differently on a 4K display and a
 * phone, while a movement count behaves the same on both because it counts
 * intent rather than pixels (§21.2).
 */
export const DETENT_BREAKOUT_MOVES = 6;

function byPosition(a: FaderLawPoint, b: FaderLawPoint): number {
  return a.position - b.position;
}

function clamp01(value: number): number {
  if (!Number.isFinite(value)) return 0;
  return Math.min(1, Math.max(0, value));
}

/** The points that carry a dB value, in travel order. */
function numericPoints(law: FaderLaw): { position: number; db: number }[] {
  return law
    .filter((point): point is FaderLawPoint & { db: number } => typeof point.db === "number")
    .map((point) => ({ position: point.position, db: point.db }))
    .sort(byPosition);
}

/** The top of the off region: the highest point the driver published as `db: null`. */
function offPosition(law: FaderLaw): number | null {
  let top: number | null = null;
  for (const point of law) {
    if (point.db === null && (top === null || point.position > top)) top = point.position;
  }
  return top;
}

function interpolate(x: number, x0: number, y0: number, x1: number, y1: number): number {
  if (x1 === x0) return y0;
  return y0 + ((x - x0) * (y1 - y0)) / (x1 - x0);
}

/**
 * Position to dB by linear interpolation between the table's points.
 *
 * At or below the `db: null` point the fader is off and this returns null. The
 * short stretch between that point and the driver's lowest published dB is
 * continued at the slope of the segment above it rather than clamped, so the
 * conversion stays invertible over the whole of travel — a clamped region
 * would make two different positions report the same dB, and the thumb would
 * jump on the way back. With the twenty to fifty points §5.5 expects, that
 * stretch is a sliver.
 */
export function positionToDb(law: FaderLaw, position: number): number | null {
  const points = numericPoints(law);
  if (points.length === 0) return null;
  const at = clamp01(position);
  const off = offPosition(law);
  if (off !== null && at <= off) return null;

  const first = points[0];
  const last = points[points.length - 1];
  if (!first || !last) return null;
  if (points.length === 1) return first.db;

  if (at <= first.position) {
    const next = points[1];
    if (!next) return first.db;
    return interpolate(at, first.position, first.db, next.position, next.db);
  }
  if (at >= last.position) {
    const previous = points[points.length - 2];
    if (!previous) return last.db;
    return interpolate(at, previous.position, previous.db, last.position, last.db);
  }
  for (let i = 1; i < points.length; i += 1) {
    const low = points[i - 1];
    const high = points[i];
    if (!low || !high) continue;
    if (at <= high.position) return interpolate(at, low.position, low.db, high.position, high.db);
  }
  return last.db;
}

/**
 * dB to position, the inverse of the above. `null` is off and lands at the
 * bottom of travel. A dB below the extrapolated bottom clamps there, which
 * reads as off — it is below the bottom of this desk's travel, and there is
 * nowhere else to put it.
 */
export function dbToPosition(law: FaderLaw, db: number | null): number {
  const points = numericPoints(law);
  const off = offPosition(law);
  if (db === null || points.length === 0) return off ?? 0;

  const first = points[0];
  const last = points[points.length - 1];
  if (!first || !last) return off ?? 0;
  if (points.length === 1) return clamp01(first.position);

  if (db <= first.db) {
    const next = points[1];
    if (!next) return clamp01(first.position);
    return clamp01(interpolate(db, first.db, first.position, next.db, next.position));
  }
  if (db >= last.db) {
    const previous = points[points.length - 2];
    if (!previous) return clamp01(last.position);
    return clamp01(interpolate(db, previous.db, previous.position, last.db, last.position));
  }
  for (let i = 1; i < points.length; i += 1) {
    const low = points[i - 1];
    const high = points[i];
    if (!low || !high) continue;
    if (db <= high.db) return clamp01(interpolate(db, low.db, low.position, high.db, high.position));
  }
  return clamp01(last.position);
}

/**
 * The printed legend: the points the desk prints on its panel. Hard-coding one
 * manufacturer's scale would leave it confidently wrong on any other hardware,
 * which is worse than having no scale (§5.5).
 */
export function scaleTicks(law: FaderLaw): ScaleTick[] {
  return law
    .filter((point) => typeof point.label === "string" && point.label !== "")
    .sort(byPosition)
    .map((point) => ({
      position: point.position,
      db: point.db,
      label: point.label as string,
      detent: point.detent === true,
    }));
}

/** Positions of the points with no label: minor ticks, drawn with no text (§5.5). */
export function minorTicks(law: FaderLaw): number[] {
  return law
    .filter((point) => typeof point.label !== "string" || point.label === "")
    .sort(byPosition)
    .map((point) => point.position);
}

/** The dB values the fader holds at. A driver may declare several; lighting has none. */
export function detents(law: FaderLaw): FaderLawPoint[] {
  return law.filter((point) => point.detent === true && point.db !== null).sort(byPosition);
}

/** dB for display: one decimal, and off rendered rather than printed as a number (§22.3). */
export function formatDb(db: number | null): string {
  if (db === null) return "-∞";
  const shown = db.toFixed(1);
  return db > 0 ? `+${shown}` : shown;
}

/**
 * dB for `aria-valuetext` (§24.2's corrected example): "−5.0 decibels",
 * "0.0 decibels, unity", "off" — spoken words rather than the printed
 * readout's compact symbols, because a screen reader has no glyph for "∞"
 * or "+". `atUnity` is true only when the value sits exactly on the law's
 * unity detent; every other value omits the second clause.
 */
export function formatDbSpoken(db: number | null, atUnity: boolean = false): string {
  if (db === null) return "off";
  const shown = db.toFixed(1);
  const signed = db > 0 ? `+${shown}` : shown;
  return atUnity ? `${signed} decibels, unity` : `${signed} decibels`;
}

// -- detents ------------------------------------------------------------------------

/**
 * Detent state for one gesture. Immutable: `applyDetent` returns the next one,
 * so the logic stays pure and lives in the gesture handler — not in the store
 * and not on the server (§21.2).
 */
export interface DetentGesture {
  /** The dB of the detent currently held, null when the fader is free. */
  held: number | null;
  /** Pointer moves beyond the zone edge since the gesture was last inside it. */
  breakout: number;
}

export const FREE_GESTURE: DetentGesture = Object.freeze({ held: null, breakout: 0 });

export interface DetentMove {
  /**
   * The dB to write to `pending` and send. While held this is the detent's
   * exact value: snapping a position and converting gives 0.03 dB, snapping
   * the value gives 0.0, and the second is what a scene saves and what a hirer
   * ceiling compares against (§5.5).
   */
  db: number | null;
  /** True while the detent holds the fader: cap ring, readout colour (§21.13). */
  held: boolean;
  /** The law point being held, for the readout and aria-valuetext. */
  detent: FaderLawPoint | null;
  gesture: DetentGesture;
}

function nearestDetent(law: FaderLaw, db: number): FaderLawPoint | null {
  let best: FaderLawPoint | null = null;
  let bestDistance = Number.POSITIVE_INFINITY;
  for (const point of detents(law)) {
    const distance = Math.abs(db - (point.db as number));
    if (distance <= DETENT_ZONE_DB && distance < bestDistance) {
      best = point;
      bestDistance = distance;
    }
  }
  return best;
}

function pointFor(law: FaderLaw, db: number): FaderLawPoint | null {
  return detents(law).find((point) => point.db === db) ?? null;
}

/**
 * One pointer move (§21.2):
 *
 *   position → dB via the law
 *   → within 1.5 dB of a detent and the break-out counter has not run out:
 *        use the detent's exact dB
 *     else: use the interpolated dB
 *   → write to `pending`, send over the WebSocket
 *
 * Breaking out takes a deliberate push: each move that lands beyond the zone
 * edge counts, and the detent releases after DETENT_BREAKOUT_MOVES of them.
 * The counter resets whenever the gesture leaves the zone — on the way out, so
 * the next approach starts fresh, and on the way back in — so approaching
 * unity from either direction feels identical.
 */
export function applyDetent(law: FaderLaw, position: number, gesture: DetentGesture = FREE_GESTURE): DetentMove {
  const raw = positionToDb(law, position);
  if (raw === null) {
    return { db: null, held: false, detent: null, gesture: FREE_GESTURE };
  }
  const near = nearestDetent(law, raw);

  if (gesture.held !== null) {
    const stillInside = near !== null && near.db === gesture.held;
    if (stillInside) {
      // Back inside the zone: the push has to start again.
      return { db: gesture.held, held: true, detent: near, gesture: { held: gesture.held, breakout: 0 } };
    }
    const breakout = gesture.breakout + 1;
    if (breakout < DETENT_BREAKOUT_MOVES) {
      return {
        db: gesture.held,
        held: true,
        detent: pointFor(law, gesture.held),
        gesture: { held: gesture.held, breakout },
      };
    }
    // Broken out. The counter resets, so dragging back in snaps again.
    return { db: raw, held: false, detent: null, gesture: FREE_GESTURE };
  }

  if (near !== null) {
    return { db: near.db, held: true, detent: near, gesture: { held: near.db as number, breakout: 0 } };
  }
  return { db: raw, held: false, detent: null, gesture: FREE_GESTURE };
}
