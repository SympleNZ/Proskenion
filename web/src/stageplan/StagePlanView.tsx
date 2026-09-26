/*
 * The Stage Plan tab (spec §21.12): a thin data-loading shell around
 * `StagePlan`, matching `LightingView`'s own shape — loading, error and
 * empty states around the configuration queries, mode resolved from the
 * signed-in tier.
 */
import { Theater } from "lucide-react";

import { ApiError } from "@/api/client";
import { EmptyState, ErrorState, Skeleton } from "@/components/ui/EmptyState";
import { useLightingChannels } from "@/lighting/api";
import { useSession } from "@/session/context";

import { useLightingBars, usePatchConflicts } from "./api";
import { conflictChannelIds } from "./conflicts";
import { StagePlan } from "./StagePlan";
import type { StagePlanMode } from "./types";

function statusLine(error: unknown): string | undefined {
  return error instanceof ApiError ? `${error.status} ${error.code}` : undefined;
}

export function StagePlanView() {
  const { session } = useSession();
  const bars = useLightingBars();
  const channels = useLightingChannels();
  // Overlap warns; it never blocks (§9.1) — a failure here must not stop the
  // plan itself from rendering, so it is read leniently rather than folded
  // into the pending/error gates below.
  const conflicts = usePatchConflicts();

  const mode: StagePlanMode = session?.tier === "admin" ? "admin" : "operator";

  if (bars.isPending || channels.isPending) {
    return (
      <div className="flex flex-col gap-6" aria-busy="true" aria-label="Loading the stage plan">
        <h1 className="sr-only">Stage Plan</h1>
        <Skeleton className="h-touch w-full" />
        <Skeleton className="h-touch w-full" />
      </div>
    );
  }

  if (bars.isError || channels.isError) {
    return (
      <div className="flex flex-col gap-6">
        <h1 className="view-title">Stage Plan</h1>
        <ErrorState
          title="Could not load the stage plan"
          detail="The controller did not answer. Nothing has been changed."
          status={statusLine(bars.error ?? channels.error)}
          onRetry={() => {
            void bars.refetch();
            void channels.refetch();
          }}
        />
      </div>
    );
  }

  const allBars = bars.data?.bars ?? [];
  const allChannels = channels.data?.channels ?? [];
  const placeable = allChannels.filter((channel) => channel.visible_staff && channel.bar_id !== null && channel.position !== null);

  if (allBars.length === 0 || placeable.length === 0) {
    return (
      <div className="flex flex-col gap-6">
        <h1 className="view-title">Stage Plan</h1>
        <EmptyState
          icon={Theater}
          title="No fixtures on the stage plan"
          detail="The stage plan shows fixtures where they hang."
        />
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-4">
      <h1 className="sr-only">Stage Plan</h1>
      <StagePlan mode={mode} bars={allBars} fixtures={allChannels} conflicts={conflictChannelIds(conflicts.data?.conflicts ?? [])} />
    </div>
  );
}
