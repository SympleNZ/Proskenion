/*
 * Restart, reboot and shut down (spec §21.24, §21.27, contracts §5
 * `POST /system/restart`, `/system/reboot`, `/system/shutdown`). Shown on
 * Admin → Updates and Admin → Health: one component, so the logic lives once.
 * Three buttons that do
 * nothing an update or an OS trial does not already do somewhere else on
 * this screen — they exist for the case nothing is wrong with the software
 * and someone just wants the appliance to come back cleanly. Each names its
 * consequence before it happens, never "are you sure".
 */
import { Power, PowerOff, RefreshCw } from "lucide-react";
import { useState } from "react";

import { ApiError } from "@/api/client";
import { Card } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { ConfirmDialog } from "@/components/ui/Sheet";

import { useRebootAppliance, useRestartApplication, useShutdownAppliance } from "./api";
import { ReconnectWait } from "./ReconnectWait";

type Action = "restart" | "reboot" | "shutdown" | null;

export function RestartRebootCard() {
  const restart = useRestartApplication();
  const reboot = useRebootAppliance();
  const shutdown = useShutdownAppliance();
  const [confirming, setConfirming] = useState<Action>(null);
  const [waitMessage, setWaitMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [poweredOff, setPoweredOff] = useState(false);

  function run(action: Exclude<Action, null>): void {
    setConfirming(null);
    setError(null);
    if (action === "shutdown") {
      // Nothing brings it back, so there is no reconnection wait to hand off to.
      shutdown.mutate(undefined, {
        onSuccess: () => setPoweredOff(true),
        onError: (mutationError) =>
          setError(mutationError instanceof ApiError ? mutationError.message : "Could not shut down."),
      });
      return;
    }
    const mutation = action === "restart" ? restart : reboot;
    const message = action === "restart" ? "Restarting the application…" : "Rebooting the appliance…";
    mutation.mutate(undefined, {
      onSuccess: () => setWaitMessage(message),
      onError: (mutationError) =>
        setError(mutationError instanceof ApiError ? mutationError.message : `Could not ${action}.`),
    });
  }

  return (
    <Card title="Restart and shut down" titleLevel="h2">
      {poweredOff ? (
        <p className="text-fg-secondary text-sm" role="status">
          The controller is powering off. It will not come back on by itself — to start it again, switch its power
          off and on at the rack (or unplug and replug it).
        </p>
      ) : waitMessage ? (
        <ReconnectWait message={waitMessage} onReconnected={() => setWaitMessage(null)} />
      ) : (
        <>
          <p className="text-fg-secondary text-sm">
            None of these changes the software. Restart services gives the controller software a clean start;
            restart controller restarts the whole box and comes back by itself; shut down powers it off until
            someone switches it back on at the rack.
          </p>
          {error ? (
            <p className="field-note" role="alert">
              {error}
            </p>
          ) : null}
          <div className="flex flex-wrap gap-3">
            <Button variant="secondary" confirmTrigger helpId="updates.restart" onClick={() => setConfirming("restart")}>
              <RefreshCw aria-hidden="true" className="size-4" />
              Restart services
            </Button>
            <Button variant="secondary" confirmTrigger helpId="updates.reboot" onClick={() => setConfirming("reboot")}>
              <Power aria-hidden="true" className="size-4" />
              Restart controller
            </Button>
            <Button variant="secondary" confirmTrigger helpId="updates.shutdown" onClick={() => setConfirming("shutdown")}>
              <PowerOff aria-hidden="true" className="size-4" />
              Shut down
            </Button>
          </div>
        </>
      )}

      <ConfirmDialog
        open={confirming === "restart"}
        onOpenChange={(open) => !open && setConfirming(null)}
        title="Restart the services?"
        description="This restarts the Proskenion application. It will be unavailable for about a minute, and everyone connected — staff and hirers — will be disconnected. The operating system and the appliance itself are not affected."
        confirmLabel="Restart"
        onConfirm={() => run("restart")}
      />
      <ConfirmDialog
        open={confirming === "reboot"}
        onOpenChange={(open) => !open && setConfirming(null)}
        title="Restart the controller?"
        description="This reboots the whole appliance, including the operating system. It will be unavailable for about a minute, and everyone connected — staff and hirers — will be disconnected."
        confirmLabel="Reboot"
        destructive
        onConfirm={() => run("reboot")}
      />
      <ConfirmDialog
        open={confirming === "shutdown"}
        onOpenChange={(open) => !open && setConfirming(null)}
        title="Shut down the controller?"
        description="The controller will power off. It will not come back on by itself — to start it again, switch its power off and on at the rack (or unplug and replug it). Lighting, sound and video control stop until then."
        confirmLabel="Shut down"
        destructive
        onConfirm={() => run("shutdown")}
      />
    </Card>
  );
}
