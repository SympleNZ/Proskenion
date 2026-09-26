/*
 * The external-control banner (spec §21.11, §7.2.7): nothing while off, the
 * detected banner (no resume action — "the desk stops and the controller
 * takes over by itself"), or the manual banner with its confirmed Resume.
 */
import { useState } from "react";

import { presentError } from "@/api/errors";
import { Banner } from "@/components/ui/Banner";
import { Button } from "@/components/ui/Button";
import { ConfirmDialog } from "@/components/ui/Sheet";
import { useExternalControl } from "@/live/store";

import { useSetExternalControl } from "./api";

export function ExternalControlBanner() {
  const externalControl = useExternalControl();
  const setExternalControl = useSetExternalControl();
  const [confirmOpen, setConfirmOpen] = useState(false);

  if (externalControl === "off") return null;

  if (externalControl === "detected") {
    return (
      <Banner tone="warning" title="Under external control — booth desk">
        <p>Showing live fixture levels from the desk.</p>
        <p>Stage banks are locked out at the KNX panel.</p>
        <p>House lighting is unaffected.</p>
      </Banner>
    );
  }

  // manual — a desk patched directly to the fixtures, bypassing the node.
  return (
    <>
      <Banner
        tone="warning"
        title="DMX output suspended — set manually"
        action={
          <Button variant="secondary" onClick={() => setConfirmOpen(true)}>
            Resume controller output
          </Button>
        }
      >
        <p>No booth input detected, so fixture levels are unknown.</p>
        <p>Levels shown are the controller&rsquo;s last values.</p>
      </Banner>
      <ConfirmDialog
        open={confirmOpen}
        onOpenChange={setConfirmOpen}
        title="Resume controller output?"
        description="Resuming may cause a visible change: fixtures will jump to the controller's own levels."
        confirmLabel="Resume"
        onConfirm={() => {
          setConfirmOpen(false);
          setExternalControl.mutate(false, { onError: (error) => void presentError(error) });
        }}
      />
    </>
  );
}
