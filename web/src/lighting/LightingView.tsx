/*
 * The operator Lighting view (spec §21.11 — the wireframe there is the
 * specification for this view): the pinned header, the stage banks, the
 * Groups row and the Fixtures row, both scrolling horizontally with
 * scroll-snap.
 */
import { Lightbulb } from "lucide-react";

import { ApiError } from "@/api/client";
import { EmptyState, ErrorState, Skeleton } from "@/components/ui/EmptyState";

import { useLightingChannels, useLightingGroups } from "./api";
import { ExternalControlBanner } from "./ExternalControlBanner";
import { setFadeSeconds, useFadeSeconds } from "./fadeTime";
import { FixtureStrip } from "./FixtureStrip";
import { GroupStrip } from "./GroupStrip";
import { LightingHeader } from "./LightingHeader";
import { StageBanks } from "./StageBanks";
import type { LightingChannel, LightingGroup } from "./types";

function statusLine(error: unknown): string | undefined {
  return error instanceof ApiError ? `${error.status} ${error.code}` : undefined;
}

function firstGroupColour(channel: LightingChannel, byId: ReadonlyMap<number, string>): string | undefined {
  const firstId = channel.group_ids[0];
  return firstId !== undefined ? byId.get(firstId) : undefined;
}

export function LightingView() {
  const channels = useLightingChannels();
  const groups = useLightingGroups();
  // Shared with the stage plan's "Set level" action (§21.12) — one fade time,
  // read by both views, rather than two independent copies that could disagree.
  const fadeSeconds = useFadeSeconds();

  if (channels.isPending || groups.isPending) {
    return (
      <div className="lighting-view" aria-busy="true" aria-label="Loading lighting">
        <h1 className="sr-only">Lighting</h1>
        <Skeleton className="h-touch w-full" />
        <Skeleton className="h-touch w-full" />
      </div>
    );
  }

  if (channels.isError || groups.isError) {
    return (
      <div className="lighting-view">
        <h1 className="view-title">Lighting</h1>
        <ErrorState
          title="Could not load lighting"
          detail="The controller did not answer. Nothing has been changed."
          status={statusLine(channels.error ?? groups.error)}
          onRetry={() => {
            void channels.refetch();
            void groups.refetch();
          }}
        />
      </div>
    );
  }

  const allChannels: readonly LightingChannel[] = channels.data?.channels ?? [];
  const allGroups: readonly LightingGroup[] = [...(groups.data?.groups ?? [])].sort((a, b) => a.sort_order - b.sort_order);

  if (allChannels.length === 0) {
    return (
      <div className="lighting-view">
        <h1 className="view-title">Lighting</h1>
        <EmptyState icon={Lightbulb} title="No fixtures configured" detail="Registered fixtures and groups appear here." />
      </div>
    );
  }

  const groupNames = new Map(allGroups.map((group) => [group.id, group.name] as const));
  const groupColourById = new Map(allGroups.map((group) => [group.id, group.colour] as const));
  const channelsByGroup = new Map<number, LightingChannel[]>(allGroups.map((group) => [group.id, []]));
  for (const channel of allChannels) {
    for (const groupId of channel.group_ids) {
      channelsByGroup.get(groupId)?.push(channel);
    }
  }

  return (
    <div className="lighting-view">
      <h1 className="sr-only">Lighting</h1>
      <LightingHeader fadeSeconds={fadeSeconds} onFadeSecondsChange={setFadeSeconds} />
      {/* The Fade slider above writes straight into the shared store (§21.12); the
          setter is stable, so passing it as the change handler is only ever the
          one write, not a local echo that could drift from the stage plan's copy. */}
      <ExternalControlBanner />
      <StageBanks />
      {allGroups.length > 0 ? (
        <section>
          <h2 className="lighting-section-title">Groups</h2>
          <div className="lighting-scroller">
            {allGroups.map((group) => (
              <GroupStrip key={group.id} group={group} channels={channelsByGroup.get(group.id) ?? []} groupNames={groupNames} />
            ))}
          </div>
        </section>
      ) : null}
      <section>
        <h2 className="lighting-section-title">Fixtures</h2>
        <div className="lighting-scroller">
          {allChannels.map((channel) => (
            <FixtureStrip key={channel.id} channel={channel} accentColour={firstGroupColour(channel, groupColourById)} />
          ))}
        </div>
      </section>
    </div>
  );
}
