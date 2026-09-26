/*
 * What the shell shows during a WebSocket outage (spec §10.7, §21.7): a
 * reconnecting banner over a dimmed surface with last-known values, and after
 * five minutes a full-screen connection-lost overlay with a manual retry.
 *
 * The values on screen during an outage are cached, and say so (§21.27:
 * "last-known values with a cached label"). That holds whether they were
 * still in memory when the connection dropped or were put back from the last
 * saved copy after a cold start while the controller was unreachable
 * (`@/offline/lastKnown`, drawn from the service worker's cached shell).
 *
 * A socket closed with 4001 is not an outage: the build on screen no longer
 * speaks the server's protocol, and the overlay says "Refresh required"
 * instead (§16.8) — reconnecting cannot help, only loading the new build can.
 */
import { History, RefreshCw, WifiOff } from "lucide-react";

import { Banner } from "@/components/ui/Banner";
import { Button } from "@/components/ui/Button";
import { REFRESH_REQUIRED_MESSAGE, setConnectionState, useConnectionFault, useConnectionState } from "@/live/connection";
import { applyUpdate, bypassServiceWorker } from "@/pwa/serviceWorker";

/** §21.27's cached label: icon and text, never colour alone (§24.1). */
export function CachedLabel() {
  return (
    <span className="cached-label">
      <History aria-hidden="true" className="size-4" />
      Cached values
    </span>
  );
}

export function ConnectionBanner() {
  const state = useConnectionState();
  const fault = useConnectionFault();
  if (state === "connected") return null;
  if (state === "reconnecting") {
    return (
      <Banner
        tone="warning"
        title={
          <>
            Reconnecting to the controller… <CachedLabel />
          </>
        }
      >
        Values shown are last-known. Controls are disabled until the connection returns.
      </Banner>
    );
  }
  if (fault === "refresh_required") {
    return (
      <div className="session-backdrop" role="presentation">
        <div className="session-dialog" role="alertdialog" aria-modal="true" aria-labelledby="connection-lost-title">
          <h2 id="connection-lost-title">
            <RefreshCw aria-hidden="true" className="text-warning-text" />
            Refresh required
          </h2>
          <p>{REFRESH_REQUIRED_MESSAGE}</p>
          <Button variant="primary" onClick={() => void applyUpdate()}>
            Refresh
          </Button>
        </div>
      </div>
    );
  }
  return (
    <div className="session-backdrop" role="presentation">
      <div className="session-dialog" role="alertdialog" aria-modal="true" aria-labelledby="connection-lost-title">
        <h2 id="connection-lost-title">
          <WifiOff aria-hidden="true" className="text-danger-text" />
          Connection lost
        </h2>
        <p>The controller has not answered for five minutes. Check that this device is on the venue network.</p>
        <p>
          <CachedLabel />
        </p>
        <Button variant="primary" onClick={() => setConnectionState("reconnecting")}>
          Try again
        </Button>
        {/* Past the offline copy: if the controller is up but serving
            something else (§4.6's recovery page), this is how to see it. */}
        <Button variant="secondary" onClick={() => void bypassServiceWorker()}>
          Reload from the controller
        </Button>
      </div>
    </div>
  );
}
