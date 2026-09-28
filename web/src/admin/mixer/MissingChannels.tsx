/*
 * "Add missing channels" (§7.3, §21.21): every channel on the desk exists in
 * admin by default — a mixer is given one per desk channel when it is added,
 * and what a hirer sees is decided by their pages, not by which channels
 * exist. A mixer configured before that, or one whose driver has just
 * changed, can lack some; this adds a channel for each desk channel no
 * channel covers, named from the desk's own labels and visible to staff.
 * Existing channels are never renamed, reordered, re-pointed or deleted.
 *
 * Two places offer it: the Mixer screen, whenever something is missing, and
 * the re-mapping sheet once a driver change is applied (§5.5), which owns
 * that moment rather than adding channels on its own.
 */
import { useState } from "react";

import { presentError } from "@/api/errors";
import { Banner } from "@/components/ui/Banner";
import { Button } from "@/components/ui/Button";

import { useAddMissingChannels, useMissingChannels } from "./api";

function plural(count: number, one: string, many: string): string {
  return `${count} ${count === 1 ? one : many}`;
}

/** The Mixer screen's banner: shown only while the desk has channels no channel covers. */
export function MissingChannelsBanner({ deviceId }: { deviceId: number }) {
  const missing = useMissingChannels(deviceId);
  const add = useAddMissingChannels();
  const [added, setAdded] = useState<number | null>(null);
  const refs = missing.data?.missing ?? [];

  if (refs.length === 0) {
    return added !== null ? (
      <Banner tone="success">
        Added {plural(added, "channel", "channels")}. Rename them, or untick Staff on any operators should not see.
      </Banner>
    ) : null;
  }

  return (
    <Banner
      tone="info"
      title={`${plural(refs.length, "desk channel has", "desk channels have")} no channel here`}
      action={
        <Button
          variant="primary"
          helpId="mixer.channels.add-missing"
          loading={add.isPending}
          onClick={() =>
            add.mutate(deviceId, {
              onSuccess: (response) => setAdded(response.created.length),
              onError: (error) => presentError(error),
            })
          }
        >
          Add missing channels
        </Button>
      }
    >
      {refs.map((ref) => ref.label).join(", ")}. Adding them leaves every existing channel as it is.
    </Banner>
  );
}

export interface AddMissingOfferProps {
  deviceId: number;
  deviceName: string;
  count: number;
  /** Called with what happened, once the admin adds them or declines. */
  onDone: (message: string | null) => void;
}

/** The re-mapping sheet's offer, after a driver change is applied (§5.5). */
export function AddMissingOffer({ deviceId, deviceName, count, onDone }: AddMissingOfferProps) {
  const add = useAddMissingChannels();
  return (
    <>
      <p className="field-help">
        The new driver has {plural(count, "desk channel", "desk channels")} that no channel of {deviceName} points at. Add a
        channel for each, named from the desk and visible to staff? Nothing already configured changes.
      </p>
      <div className="dialog-actions">
        <Button variant="secondary" onClick={() => onDone(null)} disabled={add.isPending}>
          Not now
        </Button>
        <Button
          variant="primary"
          helpId="mixer.channels.add-missing"
          loading={add.isPending}
          onClick={() =>
            add.mutate(deviceId, {
              onSuccess: (response) => onDone(`Added ${plural(response.created.length, "channel", "channels")}.`),
              onError: (error) => presentError(error),
            })
          }
        >
          Add missing channels
        </Button>
      </div>
    </>
  );
}
