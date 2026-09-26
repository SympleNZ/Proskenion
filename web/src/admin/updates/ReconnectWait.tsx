/*
 * The wait after an action that restarts the application or reboots the
 * appliance (§21.24, §21.27, §10.8): every action on this screen either does
 * one or the other, so this is one component the drop-zone apply, the
 * rollback, the OS trial, restart and reboot all hand off to.
 *
 * Unlike the network change's reconnection flow (`admin/network/reconnect.ts`),
 * this never crosses an origin — the address does not change — so there is
 * no confirm token and no `/reconnect` static page to visit. It polls the
 * unauthenticated, unversioned `/health` liveness endpoint directly
 * (`api/system.py`: "external monitors and the reconnection screen should
 * not have to track API versions") until it answers, then hands back to the
 * caller to refresh whatever state it cares about.
 *
 * A short grace period before the first poll keeps a fast restart from
 * reading as "already back" before the old process has actually gone away.
 */
import { useEffect } from "react";
import { Loader2 } from "lucide-react";

import { api } from "@/api/client";

const GRACE_MS = 3_000;
const POLL_MS = 2_000;

export interface ReconnectWaitProps {
  /** Why the wait is happening, e.g. "Restarting the application…". */
  message: string;
  onReconnected: () => void;
}

export function ReconnectWait({ message, onReconnected }: ReconnectWaitProps) {
  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const poll = () => {
      void api<{ status: string }>("/health", { absolute: true, quiet: true })
        .then(() => {
          if (!cancelled) onReconnected();
        })
        .catch(() => {
          if (!cancelled) timer = setTimeout(poll, POLL_MS);
        });
    };

    timer = setTimeout(poll, GRACE_MS);
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- onReconnected is a stable handler; re-running the effect on every render would restart the grace period
  }, []);

  return (
    <div className="reconnect-wait" role="status" aria-live="polite">
      <Loader2 aria-hidden="true" className="size-8" strokeWidth={2.5} />
      <p className="text-fg">{message}</p>
      <p className="text-fg-muted text-sm">
        This will take about a minute. This screen will update on its own — there is nothing else to do.
      </p>
    </div>
  );
}
