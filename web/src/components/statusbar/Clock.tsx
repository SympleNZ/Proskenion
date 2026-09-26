/*
 * The clock is the appliance's time, not the browser's (spec §21.7). Shown to
 * the minute from server_time plus the measured offset. Where the two differ
 * by more than a minute it carries an amber marker and the sheet says which
 * is which.
 */
import { TriangleAlert } from "lucide-react";
import { useState } from "react";

import { Sheet, SheetContent } from "@/components/ui/Sheet";
import { clocksDisagree, formatClock } from "@/lib/time";
import { useNow } from "@/lib/useNow";
import { useSession } from "@/session/context";

export function Clock() {
  const { serverTimeOffset } = useSession();
  const now = useNow();
  const [open, setOpen] = useState(false);

  const appliance = new Date(now + serverTimeOffset);
  const browser = new Date(now);
  const differs = clocksDisagree(serverTimeOffset);

  return (
    <Sheet open={open} onOpenChange={setOpen}>
      <button
        type="button"
        className="clock"
        onClick={() => setOpen(true)}
        aria-label={`Appliance time ${formatClock(appliance)}${differs ? ", differs from this device's clock" : ""}`}
      >
        <time dateTime={appliance.toISOString()}>{formatClock(appliance)}</time>
        {differs && (
          <span className="clock-marker" data-testid="clock-skew" title="Differs from this device's clock">
            <TriangleAlert aria-hidden="true" className="size-4" />
          </span>
        )}
      </button>
      <SheetContent title="Clock" description="Scheduled rules fire against the appliance's clock, not this device's.">
        <dl className="kv">
          <dt>Appliance</dt>
          <dd className="technical">{formatClock(appliance)}</dd>
          <dt>This device</dt>
          <dd className="technical">{formatClock(browser)}</dd>
        </dl>
        {differs ? (
          <p className="note text-warning-text" role="status">
            The two clocks differ by more than a minute. The appliance's time is the one that counts.
          </p>
        ) : (
          <p className="note">The two clocks agree.</p>
        )}
      </SheetContent>
    </Sheet>
  );
}
