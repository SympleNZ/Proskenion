/*
 * Admin → Lighting → Stage Plan (spec §21.18): the same `StagePlan` used at
 * `/app/stage-plan`, hosted here in admin mode with the extra
 * configuration data its fixture sheet needs — fixture profiles, lighting
 * output devices and KNX addresses — that the lighter operational
 * `StagePlanView` wrapper does not fetch. One `StagePlan` component, two thin
 * data-loading shells around it, exactly the existing `StagePlanView` /
 * `LightingView` pattern.
 */
import { Theater } from "lucide-react";

import { useDevices } from "@/admin/devices/api";
import { ApiError } from "@/api/client";
import { EmptyState, ErrorState, Skeleton } from "@/components/ui/EmptyState";
import { useLightingChannels, useLightingGroups } from "@/lighting/api";
import { conflictChannelIds } from "@/stageplan/conflicts";
import { useLightingBars, usePatchConflicts } from "@/stageplan/api";
import { StagePlan } from "@/stageplan/StagePlan";

import { useFixtureProfiles, useKnxAddresses } from "./api";

function statusLine(error: unknown): string | undefined {
  return error instanceof ApiError ? `${error.status} ${error.code}` : undefined;
}

export function StagePlanTab() {
  const bars = useLightingBars();
  const channels = useLightingChannels();
  const profiles = useFixtureProfiles();
  const devices = useDevices();
  const groups = useLightingGroups();
  const knxAddresses = useKnxAddresses();
  // Overlap warns; it never blocks (§9.1) — read leniently, like `StagePlanView`.
  const conflicts = usePatchConflicts();

  if (bars.isPending || channels.isPending || profiles.isPending || devices.isPending || groups.isPending) {
    return (
      <div className="flex flex-col gap-6" aria-busy="true" aria-label="Loading the stage plan">
        <Skeleton className="h-touch w-full" />
        <Skeleton className="h-touch w-full" />
      </div>
    );
  }

  if (bars.isError || channels.isError || profiles.isError || devices.isError || groups.isError) {
    return (
      <ErrorState
        title="Could not load the stage plan"
        detail="The controller did not answer. Nothing has been changed."
        status={statusLine(bars.error ?? channels.error ?? profiles.error ?? devices.error ?? groups.error)}
        onRetry={() => {
          void bars.refetch();
          void channels.refetch();
          void profiles.refetch();
          void devices.refetch();
          void groups.refetch();
        }}
      />
    );
  }

  const allBars = bars.data?.bars ?? [];
  const allChannels = channels.data?.channels ?? [];
  const lightingDevices = (devices.data?.devices ?? []).filter((device) => device.category === "lighting");
  const placeable = allChannels.filter((channel) => channel.visible_staff && channel.bar_id !== null && channel.position !== null);

  if (allBars.length === 0 || placeable.length === 0) {
    return (
      <EmptyState
        icon={Theater}
        title="No fixtures on the stage plan"
        detail="The stage plan shows fixtures where they hang. Add a bar and a fixture to get started."
      />
    );
  }

  return (
    <StagePlan
      mode="admin"
      bars={allBars}
      fixtures={allChannels}
      conflicts={conflictChannelIds(conflicts.data?.conflicts ?? [])}
      profiles={profiles.data?.profiles ?? []}
      devices={lightingDevices}
      knxAddresses={knxAddresses.data ?? []}
      groups={groups.data?.groups ?? []}
    />
  );
}
