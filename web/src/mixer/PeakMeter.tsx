/*
 * The mixer meter (spec §21.13 "Metering", §5.5, §5.6 B58). One narrow bar
 * per driver reference — two side by side for a stereo channel — separate
 * from the fader's own gradient so position and level are never confused.
 * Meters are display only: nothing here is written to the pending overlay,
 * nothing here is read by anything that could act on it, and fader position
 * never drives a bar (CONVENTIONS "Interface").
 *
 * Colour follows the printed marks exactly — green below −10 dB, amber to
 * 0 dB, red above — computed from the driver's own fader law so the meter
 * reads the same whether the operator looks at the colour or the scale
 * (§21.13). Peak hold is client-side ballistics from `peakHold.ts`.
 */
import { useEffect, useState } from "react";

import { dbToPosition, type FaderLaw } from "@/lib/faderLaw";

import { advancePeak, NO_PEAK, type PeakState } from "./peakHold";

const GREEN_TO_AMBER_DB = -10;
const AMBER_TO_RED_DB = 0;

function now(): number {
  return typeof performance !== "undefined" ? performance.now() : Date.now();
}

function clampPercent(position: number): string {
  return `${Math.min(100, Math.max(0, position * 100)).toFixed(2)}%`;
}

/** The fixed-band gradient, positioned at wherever the law puts −10 dB and 0 dB (§21.13). */
function bandGradient(law: FaderLaw): string {
  const greenTop = Math.min(100, Math.max(0, dbToPosition(law, GREEN_TO_AMBER_DB) * 100));
  const amberTop = Math.min(100, Math.max(greenTop, dbToPosition(law, AMBER_TO_RED_DB) * 100));
  return (
    `linear-gradient(to top, var(--color-success) 0%, var(--color-success) ${greenTop}%, ` +
    `var(--color-warning) ${greenTop}%, var(--color-warning) ${amberTop}%, ` +
    `var(--color-danger) ${amberTop}%, var(--color-danger) 100%)`
  );
}

/**
 * Peak-hold ballistics driven by a render loop (§21.2); the math itself is
 * `advancePeak`, tested on its own. The functional updater form reads the
 * latest state directly from React on every tick, so there is no ref to
 * keep in sync and no stale closure to worry about.
 */
function usePeakHold(value: number | null): number | null {
  const [state, setState] = useState<PeakState>(NO_PEAK);
  useEffect(() => {
    let frame: number;
    const tick = (): void => {
      setState((previous) => advancePeak(previous, value, now()));
      frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [value]);
  return state.peak;
}

interface MeterBarProps {
  value: number | null;
  law: FaderLaw;
  gradient: string;
}

function MeterBar({ value, law, gradient }: MeterBarProps) {
  const peak = usePeakHold(value);
  const fillHeight = value !== null ? clampPercent(dbToPosition(law, value)) : "0%";
  const peakBottom = peak !== null ? clampPercent(dbToPosition(law, peak)) : null;
  return (
    <div className="mixer-meter-bar" data-testid="meter-bar">
      <div className="mixer-meter-track" style={{ background: gradient }}>
        <div className="mixer-meter-cover" style={{ height: `calc(100% - ${fillHeight})` }} />
        {peakBottom !== null ? <div className="mixer-meter-peak" style={{ bottom: peakBottom }} /> : null}
      </div>
    </div>
  );
}

export interface PeakMeterProps {
  /** One entry per driver reference, index 0 = left (§5.5). `null` when this channel has no meter data at all (B58). */
  values: readonly (number | null)[] | null;
  law: FaderLaw;
  /** How many bars the channel meters with, for the slot drawn while no data has arrived. */
  stereo?: boolean;
}

/**
 * Rendered only while metering is available — the caller omits it
 * otherwise, so the bar is absent, not empty, and the fader re-centres
 * (§21.13). With data: one bar per driver reference, and a hairline at the
 * law's 0 dB, so the meter reads against the same reference as the scale.
 *
 * Metering available but no reading yet for this channel: the slot keeps
 * its place, so every strip in a row has the same shape, drawn as a dashed
 * outline with nothing inside. Never a dark, empty track: that reads as
 * silence, a claim about the signal this interface has no data for (B58
 * "absent, not zero").
 */
export function PeakMeter({ values, law, stereo = false }: PeakMeterProps) {
  if (!values || values.length === 0) {
    const count = stereo ? 2 : 1;
    return (
      <div className="mixer-meter" data-state="no-data" data-bars={count} title="No meter reading from the mixer for this channel" aria-hidden="true">
        {Array.from({ length: count }, (_, index) => (
          <div key={index} className="mixer-meter-bar" data-testid="meter-slot-empty" />
        ))}
      </div>
    );
  }
  const gradient = bandGradient(law);
  const zero = clampPercent(dbToPosition(law, AMBER_TO_RED_DB));
  return (
    <div className="mixer-meter" data-bars={values.length} aria-hidden="true">
      {values.map((value, index) => (
        // A fixed, ordered set of driver references (§5.5) — never reordered or filtered — so the index is a stable key.
        <MeterBar key={index} value={value} law={law} gradient={gradient} />
      ))}
      <div className="mixer-meter-zero" style={{ bottom: zero }} />
    </div>
  );
}
