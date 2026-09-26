/*
 * `hirer_max_db` as a dB control (§21.21, CONVENTIONS "dB in the interface,
 * never a mixer's wire value"). `null` is "no ceiling" (§5.5) — the slider
 * only appears once a ceiling is switched on, so there is no dB value that
 * secretly means "off" the way a raw NRPN minimum would.
 *
 * Positioned against the driver's own fader law (§5.5 "The fader law is
 * published as data"), the same law the operator's own faders use, so the
 * ceiling's slider travel matches what the venue's fader actually does —
 * not a generic 0–100 scale reinterpreted as decibels. Where no law has
 * loaded yet (the device is still being fetched, or has none), a plain
 * numeric dB field is offered instead of a disabled control.
 */
import { useId } from "react";

import { Checkbox } from "@/components/ui/Select";
import { HelpButton } from "@/help/HelpButton";
import { formatDb, type FaderLawPoint } from "@/lib/faderLaw";

import { lawFaderScale } from "@/components/fader/FaderScale";

/** The venue's own default ceiling is about −5 dB (§7.3: NRPN 10000 ≈ −5 dB). */
const DEFAULT_CEILING_DB = -5;

export interface HirerCeilingControlProps {
  id: string;
  value: number | null;
  onChange: (next: number | null) => void;
  law: readonly FaderLawPoint[];
}

export function HirerCeilingControl({ id, value, onChange, law }: HirerCeilingControlProps) {
  const noneId = useId();
  const hasCeiling = value !== null;
  const scale = law.length > 0 ? lawFaderScale(law) : null;

  function toggleCeiling(enabled: boolean): void {
    if (!enabled) {
      onChange(null);
      return;
    }
    const fallback = scale ? Math.min(scale.max, Math.max(scale.min, DEFAULT_CEILING_DB)) : DEFAULT_CEILING_DB;
    onChange(value ?? fallback);
  }

  return (
    <div className="field schema-field">
      <div className="field-label-row">
        <span className="field-label" id={`${id}-label`}>
          Hirer maximum
        </span>
        <HelpButton id="mixer.channel.hirer-max" />
      </div>
      <Checkbox id={noneId} label="No ceiling" checked={!hasCeiling} onChange={(event) => toggleCeiling(!event.currentTarget.checked)} />
      {hasCeiling && value !== null ? (
        scale ? (
          <div className="connection-row">
            <input
              id={id}
              type="range"
              aria-labelledby={`${id}-label`}
              min={0}
              max={1000}
              step={1}
              value={Math.round(scale.toPosition(value) * 1000)}
              onChange={(event) => onChange(scale.fromPosition(Number(event.currentTarget.value) / 1000))}
            />
            <span className="technical">{scale.format(value)}</span>
          </div>
        ) : (
          <div className="connection-row">
            <input
              id={id}
              className="input input-mono"
              type="number"
              step={0.1}
              aria-labelledby={`${id}-label`}
              value={value}
              onChange={(event) => onChange(Number(event.currentTarget.value))}
            />
            <span className="technical">{formatDb(value)}</span>
          </div>
        )
      ) : null}
    </div>
  );
}
