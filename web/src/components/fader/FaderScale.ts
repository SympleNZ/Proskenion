/*
 * A pluggable fader scale (spec §21.2, §21.5, §5.5, §10.3). `FaderStrip` never
 * hard-codes a law: it takes a `FaderScale` and draws from it exclusively —
 * value ↔ position, the printed legend, keyboard steps and detent snapping.
 *
 * Lighting passes `linearLightingScale()`: 0–100, one decimal, no detents — a
 * detent at 100% would fight the most common movement (§21.2). The mixer will
 * pass `lawFaderScale(law)`, built on `faderLaw.ts`; it is tested here with a
 * fabricated law carrying a unity detent so Phase 4 only has to pass one in.
 */
import {
  applyDetent,
  dbToPosition,
  detents as lawDetents,
  formatDb,
  formatDbSpoken,
  FREE_GESTURE,
  minorTicks as lawMinorTicks,
  scaleTicks,
  type FaderLaw,
} from "@/lib/faderLaw";

/** One entry of a fader's printed legend. */
export interface FaderScaleTick {
  /** 0.0 (bottom of travel) to 1.0 (top). */
  position: number;
  label: string;
  /** A detent — unity on a mixer — printed longer and brighter (§21.13). */
  detent?: boolean;
}

/**
 * Value ↔ position and everything a `FaderStrip` draws from a law. `value` is
 * always a core unit — lighting 0–100, mixer dB — never a position (§5.5,
 * B41): the component converts to position only when drawing.
 *
 * `value` is `number | null` throughout, never merely `number`: a mixer
 * channel's dB is `null` for off (§5.5, B41), reachable by dragging to the
 * very bottom of travel exactly as a real desk's fader is, and every method
 * here has to carry that value faithfully rather than coercing it to a
 * numeric floor. Lighting's own scale never produces or receives `null` — it
 * has nothing to be "off" from — but shares the one interface.
 */
export interface FaderScale {
  readonly min: number;
  readonly max: number;
  /** Value to position, 0.0 (bottom) to 1.0 (top). `null` (off) is the bottom of travel. */
  toPosition(value: number | null): number;
  /** Position to value, with any detent snap already applied (§21.2); `null` at and below the off point. */
  fromPosition(position: number): number | null;
  /** The printed readout's text (§24.2's compact form; lighting's own percentage). */
  format(value: number | null): string;
  /**
   * The spoken value for `aria-valuetext` (§24.2's corrected example:
   * "−5.0 decibels", "0.0 decibels, unity", "off"), when it differs from the
   * printed readout. Absent — as for lighting — `FaderStrip` falls back to
   * `format`, whose own text is already speakable ("82.5%").
   */
  spokenFormat?(value: number | null): string;
  /** One Arrow Up/Down step (§24.2): mixer ±1 dB, lighting ±1%. */
  keyStep(value: number | null, direction: 1 | -1): number | null;
  /** One Page Up/Down step (§24.2): mixer ±5 dB, lighting ±10%. */
  keyPageStep(value: number | null, direction: 1 | -1): number | null;
  /** The labelled points of the printed scale (§5.5 "the table also produces the printed scale"). */
  ticks(): readonly FaderScaleTick[];
  /** Unlabelled tick positions. */
  minorTicks(): readonly number[];
  /** Whether `value` sits exactly on a detent — the cap ring and readout colour (§21.13). */
  isDetent(value: number | null): boolean;
}

function clamp(value: number, min: number, max: number): number {
  if (!Number.isFinite(value)) return min;
  return Math.min(max, Math.max(min, value));
}

// -- Lighting: linear 0–100, one decimal, no detents (§21.2, §9.2) ------------------

export const LIGHTING_MIN = 0;
export const LIGHTING_MAX = 100;

function round1(value: number): number {
  return Math.round(value * 10) / 10;
}

/**
 * 0–100 linear, one decimal, nothing to return to. A detent at 100% would
 * fight the most common movement — bringing a fixture to full — so lighting
 * carries none (§21.2).
 */
export function linearLightingScale(): FaderScale {
  const span = LIGHTING_MAX - LIGHTING_MIN;
  // Lighting never actually holds null — it has nothing to be "off" from —
  // so every method treats it defensively as the bottom of travel, purely to
  // satisfy the one interface it shares with the mixer's law-based scale.
  const orMin = (value: number | null): number => value ?? LIGHTING_MIN;
  return {
    min: LIGHTING_MIN,
    max: LIGHTING_MAX,
    toPosition: (value) => clamp((orMin(value) - LIGHTING_MIN) / span, 0, 1),
    fromPosition: (position) => round1(clamp(position, 0, 1) * span + LIGHTING_MIN),
    format: (value) => `${round1(orMin(value)).toFixed(1)}%`,
    keyStep: (value, direction) => clamp(round1(orMin(value) + direction * 1), LIGHTING_MIN, LIGHTING_MAX),
    keyPageStep: (value, direction) => clamp(round1(orMin(value) + direction * 10), LIGHTING_MIN, LIGHTING_MAX),
    ticks: () => [0, 25, 50, 75, 100].map((value) => ({ position: value / 100, label: `${value}%` })),
    minorTicks: () => [10, 20, 30, 40, 60, 70, 80, 90].map((value) => value / 100),
    isDetent: () => false,
  };
}

// -- Mixer: a driver's published law, built on faderLaw.ts (§5.5, §21.2) ------------

/**
 * A scale over a `FaderLaw`. Detent snapping reuses `applyDetent` from
 * `faderLaw.ts` with a fresh gesture each call, so a position in the zone
 * always yields the detent's exact dB (§21.2 "snap to the dB, never to the
 * position"). The break-out counter — a deliberate push before the detent
 * releases — belongs to the drag itself, threaded move to move by whatever
 * holds the gesture; this scale is the law Phase 4 passes in, not the
 * gesture loop.
 */
export function lawFaderScale(law: FaderLaw): FaderScale {
  const detentValues = new Set(lawDetents(law).map((point) => point.db));
  const numeric = law.filter((point): point is typeof point & { db: number } => typeof point.db === "number");
  const dbValues = numeric.map((point) => point.db);
  const min = dbValues.length > 0 ? Math.min(...dbValues) : 0;
  const max = dbValues.length > 0 ? Math.max(...dbValues) : 0;
  // Stepping up out of off has nowhere to count from, so it lands on the
  // lowest dB the law publishes; stepping down while already off stays off —
  // there is nothing further below "off" to step to (§24.2 "at any fader
  // position").
  const stepFromOff = (direction: 1 | -1): number | null => (direction > 0 ? min : null);
  return {
    min,
    max,
    toPosition: (value) => dbToPosition(law, value),
    fromPosition: (position) => {
      // The bottom of travel is a real "off", not a floor to clamp to
      // (§5.5) — a drag that lands there must send null, exactly as typing
      // {"db": null} does over the API.
      const move = applyDetent(law, position, FREE_GESTURE);
      return move.db;
    },
    format: (value) => formatDb(value),
    // §24.2's corrected example: spoken words, not the printed readout's
    // compact symbols — a screen reader has no glyph for "∞" or "+".
    spokenFormat: (value) => formatDbSpoken(value, value !== null && detentValues.has(value)),
    keyStep: (value, direction) => (value === null ? stepFromOff(direction) : value + direction * 1),
    keyPageStep: (value, direction) => (value === null ? stepFromOff(direction) : value + direction * 5),
    ticks: () =>
      scaleTicks(law).map((tick) =>
        tick.detent ? { position: tick.position, label: tick.label, detent: true } : { position: tick.position, label: tick.label },
      ),
    minorTicks: () => lawMinorTicks(law),
    isDetent: (value) => value !== null && detentValues.has(value),
  };
}
