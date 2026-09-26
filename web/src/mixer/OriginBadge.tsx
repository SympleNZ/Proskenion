/*
 * The origin badge (spec §21.13, §7.3): teal mono text in the strip's
 * top-right corner when the most recent change to that channel came from
 * outside this view. "MixPad" for the A&H app or the desk itself, "SURFACE"
 * for the X-Touch (§7.6) — it tells the operator the fader moved without
 * them, and which surface did it.
 */
import type { MixerOrigin } from "./types";

const LABEL: Readonly<Record<"mixpad" | "surface", string>> = {
  mixpad: "MixPad",
  surface: "SURFACE",
};

export function OriginBadge({ origin }: { origin: MixerOrigin }) {
  if (origin !== "mixpad" && origin !== "surface") return null;
  return (
    <span className="mixer-origin-badge" data-origin={origin}>
      {LABEL[origin]}
    </span>
  );
}
