/*
 * The master dimmer (spec §9.5, §21.11). It scales stage (DMX) output only;
 * KNX house dimmers are outside it, so moving it sends nothing to the KNX
 * bus (§9.5). External control suspends that output, so the master is
 * read-only while external control is active, as the DMX fixture faders are
 * (§7.2.7, §21.11), and keeps showing the controller's own value. A small
 * LIVE chip appears while a desk is detected, distinguishing the fixture
 * readouts elsewhere on the view — which are observed values — from the
 * master, which is always the controller's own.
 */
import { linearLightingScale } from "@/components/fader/FaderScale";
import { FaderStrip } from "@/components/fader/FaderStrip";
import { send } from "@/live/socket";
import { beginGesture, endGesture, MASTER_KEY, useControlsEnabled, useExternalControl, useMaster } from "@/live/store";

import { isMasterReadOnlyUnderExternalControl } from "./externalControl";

const scale = linearLightingScale();

export function MasterFader() {
  const controlsEnabled = useControlsEnabled();
  const externalControl = useExternalControl();
  const value = useMaster() ?? 100;

  function handleChange(next: number | null): void {
    // linearLightingScale never actually produces null — the master has
    // nothing to be "off" from — but shares FaderStrip's nullable signature.
    if (next === null) return;
    send("master", 0, next);
  }

  return (
    <FaderStrip
      label="Master"
      value={value}
      scale={scale}
      onChange={handleChange}
      onGestureStart={() => beginGesture(MASTER_KEY)}
      onGestureEnd={() => endGesture(MASTER_KEY)}
      readOnly={isMasterReadOnlyUnderExternalControl(externalControl)}
      disabled={!controlsEnabled}
      liveChip={externalControl === "detected"}
      // §21.11's wireframe draws Master as a slider beside Fade, in the header.
      orientation="horizontal"
      className="lighting-master-fader"
      testId="master-fader"
    />
  );
}
