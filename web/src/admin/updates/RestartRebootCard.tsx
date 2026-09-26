/*
 * Admin → Updates: restart and reboot (spec §21.24, §21.27, contracts §5
 * `POST /system/restart`, `POST /system/reboot`). Two buttons that do
 * nothing an update or an OS trial does not already do somewhere else on
 * this screen — they exist for the case nothing is wrong with the software
 * and someone just wants the appliance to come back cleanly. Each names its
 * consequence before it happens, never "are you sure".
 */
import { Power, RefreshCw } from "lucide-react";
import { useState } from "react";

import { ApiError } from "@/api/client";
import { Card } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { ConfirmDialog } from "@/components/ui/Sheet";

import { useRebootAppliance, useRestartApplication } from "./api";
import { ReconnectWait } from "./ReconnectWait";

type Action = "restart" | "reboot" | null;

export function RestartRebootCard() {
  const restart = useRestartApplication();
  const reboot = useRebootAppliance();
  const [confirming, setConfirming] = useState<Action>(null);
  const [waitMessage, setWaitMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  function run(action: Exclude<Action, null>): void {
    setConfirming(null);
    setError(null);
    const mutation = action === "restart" ? restart : reboot;
    const message = action === "restart" ? "Restarting the application…" : "Rebooting the appliance…";
    mutation.mutate(undefined, {
      onSuccess: () => setWaitMessage(message),
      onError: (mutationError) =>
        setError(mutationError instanceof ApiError ? mutationError.message : `Could not ${action}.`),
    });
  }

  return (
    <Card title="Restart and reboot" titleLevel="h2">
      {waitMessage ? (
        <ReconnectWait message={waitMessage} onReconnected={() => setWaitMessage(null)} />
      ) : (
        <>
          <p className="text-fg-secondary text-sm">
            Neither of these changes the software. Use them when the application or the appliance itself needs a
            clean start.
          </p>
          {error ? (
            <p className="field-note" role="alert">
              {error}
            </p>
          ) : null}
          <div className="flex flex-wrap gap-3">
            <Button variant="secondary" confirmTrigger helpId="updates.restart" onClick={() => setConfirming("restart")}>
              <RefreshCw aria-hidden="true" className="size-4" />
              Restart application
            </Button>
            <Button variant="secondary" confirmTrigger helpId="updates.reboot" onClick={() => setConfirming("reboot")}>
              <Power aria-hidden="true" className="size-4" />
              Reboot appliance
            </Button>
          </div>
        </>
      )}

      <ConfirmDialog
        open={confirming === "restart"}
        onOpenChange={(open) => !open && setConfirming(null)}
        title="Restart the application?"
        description="This restarts the Proskenion application. It will be unavailable for about a minute, and everyone connected — staff and hirers — will be disconnected. The operating system and the appliance itself are not affected."
        confirmLabel="Restart"
        onConfirm={() => run("restart")}
      />
      <ConfirmDialog
        open={confirming === "reboot"}
        onOpenChange={(open) => !open && setConfirming(null)}
        title="Reboot the appliance?"
        description="This reboots the whole appliance, including the operating system. It will be unavailable for about a minute, and everyone connected — staff and hirers — will be disconnected."
        confirmLabel="Reboot"
        destructive
        onConfirm={() => run("reboot")}
      />
    </Card>
  );
}
