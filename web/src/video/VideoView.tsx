/*
 * The operator Video view (spec §21.14, §7.5). One panel per destination —
 * this venue has one, "The room" — with large source buttons (64 px or
 * taller, §24.2's touch minimum many times over) and the custom-routing
 * notice for a destination whose outputs have diverged at the matrix.
 *
 * A destination's routing is live state (§21.2): `GET /hdmi/state`'s
 * snapshot is the fallback until the live store holds a value, and from then
 * on the store wins, key by key, on every `hdmi_source` frame — whether that
 * frame followed our own POST or someone at the front panel. A press writes
 * the store's own pending overlay (`setPending`/`hasPending`), so the button
 * shows the selection at once and clears the instant either the REST
 * response or a frame writes to the same key — "the response or the frame",
 * whichever is first (§21.14). A rejection drops the pending overlay without
 * writing anything else, so the control falls back to whatever is already
 * confirmed there — the honest, visible-because-there-was-never-a-detour
 * version of §21.27's rollback rule.
 */
import { MonitorPlay, Video as VideoIcon } from "lucide-react";

import { ApiError } from "@/api/client";
import { presentError } from "@/api/errors";
import { EmptyState, ErrorState, Skeleton } from "@/components/ui/EmptyState";
import { clearPending, hasPending, hdmiDestinationKey, setHdmiDestinationState, setPending, useHdmiDestinationState } from "@/live/store";

import { useHdmiState, useSetHdmiSource } from "./api";
import type { HdmiDestination, HdmiInput } from "./types";

function statusLine(error: unknown): string | undefined {
  return error instanceof ApiError ? `${error.status} ${error.code}` : undefined;
}

interface DestinationPanelProps {
  destination: HdmiDestination;
  inputs: readonly HdmiInput[];
  atomicNotice: boolean;
}

function DestinationPanel({ destination, inputs, atomicNotice }: DestinationPanelProps) {
  const setSource = useSetHdmiSource();
  const live = useHdmiDestinationState(destination.id);
  const key = hdmiDestinationKey(destination.id);
  // The store's own pending overlay (§21.2, CONVENTIONS "Interface"), not a
  // local copy: `useHdmiDestinationState` already reads pending-over-
  // authoritative, so a press is visible the instant it is set and clears
  // itself the moment either the REST response or a `hdmi_source` frame
  // writes to the same key — "the response or the frame", whichever is
  // first (§21.14) — with no reconciliation logic in this component at all.
  const pending = hasPending(key);

  // `live` is `null` only before any read has landed; a resolved `inputId`
  // of `null` is a real "nothing routed" and must not fall through past.
  const activeInputId = live ? live.inputId : destination.input_id;
  const diverged = live ? live.diverged : destination.diverged;

  function press(input: HdmiInput): void {
    setPending(key, { inputId: input.id, diverged: false });
    setSource.mutate(
      { destinationId: destination.id, inputId: input.id },
      {
        onSuccess: (data) => {
          // Confirmed by PAXXR (§7.5): write it straight into live state so
          // this and every other subscribed key resolve to it at once.
          setHdmiDestinationState(data.id, { inputId: data.input_id, diverged: data.diverged });
        },
        onError: (error) => {
          // route_not_confirmed or device_unavailable (§16.1): shown plainly,
          // and the selection returns to the confirmed state by simply
          // dropping the pending overlay — nothing else moved.
          clearPending(key);
          presentError(error);
        },
      },
    );
  }

  const showDivergedNotice = diverged && !pending;

  return (
    <section className="video-destination" aria-labelledby={`video-destination-${destination.id}`}>
      <h2 id={`video-destination-${destination.id}`} className="lighting-section-title">
        {destination.name}
      </h2>
      {showDivergedNotice ? (
        <p className="video-diverged-notice" role="status">
          Custom routing set at the matrix. Select a source to restore dual-output operation.
        </p>
      ) : null}
      <div className="video-source-row">
        {inputs.map((input) => {
          const active = input.id === activeInputId;
          const isPressPending = pending && active;
          return (
            <button
              key={input.id}
              type="button"
              className="video-source-button"
              aria-pressed={active}
              aria-busy={isPressPending || undefined}
              data-pending={isPressPending || undefined}
              disabled={setSource.isPending}
              onClick={() => press(input)}
            >
              {input.name}
            </button>
          );
        })}
      </div>
      {atomicNotice ? (
        <p className="video-atomic-notice">A grouped change on this matrix may briefly show mismatched outputs (§7.5).</p>
      ) : null}
    </section>
  );
}

export function VideoView() {
  const query = useHdmiState();

  if (query.isPending) {
    return (
      <div className="video-view" aria-busy="true" aria-label="Loading video routing">
        <h1 className="sr-only">Video</h1>
        <Skeleton className="h-touch-primary w-full" />
      </div>
    );
  }

  if (query.isError) {
    return (
      <div className="video-view">
        <h1 className="view-title">Video</h1>
        <ErrorState
          title="Could not load video routing"
          detail="The controller did not answer. Nothing has been changed."
          status={statusLine(query.error)}
          onRetry={() => void query.refetch()}
        />
      </div>
    );
  }

  const data = query.data;

  if (!data || data.device_id === null) {
    return (
      <div className="video-view">
        <h1 className="view-title">Video</h1>
        <EmptyState icon={MonitorPlay} title="No matrix configured" detail="An admin configures the HDMI matrix in HDMI configuration." />
      </div>
    );
  }

  if (data.destinations.length === 0) {
    return (
      <div className="video-view">
        <h1 className="view-title">Video</h1>
        <EmptyState icon={VideoIcon} title="No destinations configured" detail="An admin adds a destination in HDMI configuration." />
      </div>
    );
  }

  return (
    <div className="video-view">
      <h1 className="view-title">Video</h1>
      {data.destinations.map((destination) => (
        <DestinationPanel
          key={destination.id}
          destination={destination}
          inputs={data.inputs}
          atomicNotice={!data.supports_atomic_route}
        />
      ))}
    </div>
  );
}
