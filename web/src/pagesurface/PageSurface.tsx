/*
 * The page surface (spec §21.9): one horizontal flow of mixer and lighting
 * strips, group trays and panels, in `sort_order`, at a shared height. It
 * takes only a resolved page object and fetches the configuration it needs
 * itself, so the hirer shell mounts it unchanged: it never assumes
 * the operator shell, and it already honours `writable: false` and
 * `ceiling_db` because `PageLightingItem`/`PageMixerItem` do.
 *
 * `hirer` is the one flag this component takes to reach §24.6's overrides —
 * it never forks into a second component. Most of §24.6 (72 px touch
 * targets) is a CSS scope (`.page-surface-hirer` in components.css) reached
 * through the class below; the one exception is the button panel's own
 * size/column search, a JS computation that needs the taller floor as a real
 * input, threaded down to `PagePanel`.
 */
import { Blocks } from "lucide-react";

import { ScrollRow } from "@/components/scrollrow/ScrollRow";
import { ErrorState, Skeleton } from "@/components/ui/EmptyState";
import { useLightingChannels } from "@/lighting/api";
import type { LightingChannel } from "@/lighting/types";
import { useElementSize } from "@/lib/useElementSize";
import { cn } from "@/lib/utils";
import { useFaderLaw, useMixerState } from "@/mixer/api";

import { channelsFromPage } from "./pageChannels";
import { PageGroupItem } from "./PageGroupItem";
import { PageLightingItem } from "./PageLightingItem";
import { PageMixerItem } from "./PageMixerItem";
import { PagePanel } from "./PagePanel";
import { isGroupMasterItem, isLightingItem, isMixerItem, isPanelItem, type PageDetail, type PageItem } from "./types";

export interface PageSurfaceProps {
  page: PageDetail;
  /** §24.6's overrides — 72 px minimum touch targets — rather than the operator's own sizing. Never set for an operator's own page. */
  hirer?: boolean;
}

function sortedItems(page: PageDetail): readonly PageItem[] {
  return [...page.items].sort((a, b) => a.sort_order - b.sort_order);
}

export function PageSurface({ page, hirer = false }: PageSurfaceProps) {
  const items = sortedItems(page);
  const needsMixer = items.some(isMixerItem);
  const needsLighting = items.some((item) => isLightingItem(item) || isGroupMasterItem(item));
  // A panel button's size/column search needs the row's own visible width
  // (§21.9) — measured once here, on the row every item shares, rather than
  // by each panel measuring itself (`PagePanel`'s own doc comment).
  const [flowRef, flowSize] = useElementSize<HTMLDivElement>();

  // Configuration state through TanStack Query (CONVENTIONS "Interface"),
  // never the live store — the same queries `MixerView`/`LightingView` hold,
  // so they are already warm by the time an operator opens Pages from either.
  const mixerState = useMixerState();
  const deviceId = mixerState.data?.device_id ?? null;
  const law = useFaderLaw(deviceId);
  // `GET /lighting/channels` is staff only (§16.5): a hirer's page resolves
  // its tray members from the page itself (`channelsFromPage`) instead.
  const lightingChannels = useLightingChannels({ enabled: !hirer });

  const mixerPending = needsMixer && (mixerState.isPending || (deviceId !== null && law.isPending));
  const lightingPending = !hirer && needsLighting && lightingChannels.isPending;
  if (mixerPending || lightingPending) {
    return (
      <div className="page-surface-flow" aria-busy="true" aria-label={`Loading ${page.name}`}>
        <Skeleton className="h-touch-primary w-full" />
      </div>
    );
  }

  const mixerErrored = needsMixer && mixerState.isError;
  const lightingErrored = !hirer && needsLighting && lightingChannels.isError;
  if (mixerErrored || lightingErrored) {
    return (
      <ErrorState
        title={`Could not load ${page.name}`}
        detail="The controller did not answer. Nothing has been changed."
        onRetry={() => {
          if (mixerErrored) void mixerState.refetch();
          if (lightingErrored) void lightingChannels.refetch();
        }}
      />
    );
  }

  if (items.length === 0) {
    return (
      <div className="page-surface-empty" aria-label={page.name}>
        <Blocks aria-hidden="true" className="size-8" strokeWidth={1.25} />
        <p>Nothing on this page yet.</p>
      </div>
    );
  }

  const channelsById = hirer
    ? channelsFromPage(items)
    : new Map<number, LightingChannel>((lightingChannels.data?.channels ?? []).map((channel) => [channel.id, channel]));
  const mixerCapabilities = mixerState.data?.capabilities;
  const mixerConnected = mixerState.data?.connected ?? false;
  const mixerLaw = law.data ?? [];

  return (
    <ScrollRow
      scrollerRef={flowRef}
      rowClassName="page-surface-row"
      className={cn("page-surface-flow", hirer && "page-surface-hirer")}
      aria-label={page.name}
    >
      {items.map((item) => {
        if (isMixerItem(item)) {
          // Every mixer item on a resolved page implies a configured device
          // (the contract would not otherwise have one to describe); the
          // guard is defensive rather than a case the interface expects.
          return mixerCapabilities ? (
            <PageMixerItem key={item.id} item={item} law={mixerLaw} capabilities={mixerCapabilities} connected={mixerConnected} hirer={hirer} />
          ) : null;
        }
        if (isLightingItem(item)) return <PageLightingItem key={item.id} item={item} />;
        if (isGroupMasterItem(item)) return <PageGroupItem key={item.id} item={item} channelsById={channelsById} />;
        if (isPanelItem(item)) return <PagePanel key={item.id} pageId={page.id} item={item} surfaceWidth={flowSize.width} hirer={hirer} />;
        return null;
      })}
    </ScrollRow>
  );
}
