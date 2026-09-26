/*
 * The pinned header (spec §21.11): Fade, Master, Save look and the External
 * control toggle with the LED treatment — amber and glowing when active,
 * unlit off. The toggle is disabled, with a tooltip saying why, while frames
 * are arriving: detection wins and the state cannot be forced off from here
 * (§7.2.7).
 *
 * Fade is a local, per-view setting — the duration the view's discrete level
 * sets (POST .../level) apply, not drags, which always go over the socket at
 * their own pace (§21.2, §21.11). No discrete "set to value" control exists
 * in this task's scope yet, so the value is held and exposed to a future one
 * rather than wired to an action here.
 *
 * Save look calls `POST /lighting/snapshot`, which §16.5 makes admin-only
 * while §21.11 draws the button in this, the operator view — so it is
 * enabled only for an admin session, matching that restriction.
 */
import { Zap } from "lucide-react";
import { useId, useState } from "react";
import { toast } from "sonner";

import { presentError } from "@/api/errors";
import { Button } from "@/components/ui/Button";
import { ConfirmDialog } from "@/components/ui/Sheet";
import { useExternalControl } from "@/live/store";
import { useSession } from "@/session/context";

import { useSaveSnapshot, useSetExternalControl } from "./api";
import { MasterFader } from "./MasterFader";

export const FADE_MIN_S = 0;
export const FADE_MAX_S = 10;
export const FADE_STEP_S = 0.1;
export const FADE_DEFAULT_S = 2;

export interface LightingHeaderProps {
  fadeSeconds: number;
  onFadeSecondsChange: (seconds: number) => void;
}

export function LightingHeader({ fadeSeconds, onFadeSecondsChange }: LightingHeaderProps) {
  const { session } = useSession();
  const externalControl = useExternalControl();
  const setExternalControl = useSetExternalControl();
  const saveSnapshot = useSaveSnapshot();
  const [confirmOpen, setConfirmOpen] = useState(false);
  const fadeId = useId();

  const isAdmin = session?.tier === "admin";
  const manual = externalControl === "manual";
  const framesArriving = externalControl === "detected";

  function handleToggleClick(): void {
    if (manual) {
      setConfirmOpen(true);
      return;
    }
    setExternalControl.mutate(true, { onError: (error) => void presentError(error) });
  }

  function handleSaveLook(): void {
    saveSnapshot.mutate(undefined, {
      onSuccess: () => toast.success("Look saved"),
      onError: (error) => void presentError(error),
    });
  }

  return (
    <header className="lighting-header">
      <div className="lighting-header-row">
        <div className="lighting-fade-control">
          <label htmlFor={fadeId}>Fade</label>
          <input
            id={fadeId}
            type="range"
            min={FADE_MIN_S}
            max={FADE_MAX_S}
            step={FADE_STEP_S}
            value={fadeSeconds}
            onChange={(event) => onFadeSecondsChange(Number(event.target.value))}
          />
          <span className="lighting-fade-value">{fadeSeconds.toFixed(1)}s</span>
        </div>
      </div>
      <div className="lighting-master-row">
        <MasterFader />
        <Button
          variant="secondary"
          disabled={!isAdmin}
          aria-disabled={!isAdmin}
          loading={saveSnapshot.isPending}
          title={isAdmin ? undefined : "Only an admin can save a look"}
          onClick={handleSaveLook}
        >
          Save look
        </Button>
        <button
          type="button"
          className="led-toggle"
          data-active={manual}
          aria-pressed={manual}
          disabled={framesArriving || setExternalControl.isPending}
          title={framesArriving ? "Can't be turned off while a desk is sending — detection wins" : undefined}
          onClick={handleToggleClick}
        >
          <span className="led-toggle-lamp" aria-hidden="true" />
          External control
          {manual ? <Zap aria-hidden="true" className="size-4" /> : null}
        </button>
      </div>
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
    </header>
  );
}
