/*
 * The hirer's device-offline banner (spec §21.15: "A device-offline banner
 * appears only when it affects controls the hirer actually has"). The status
 * bar already shows every device's indicator to every tier (§21.7); this is
 * the separate, harder-to-miss banner over the page content, and it only
 * ever names a device that sits behind something on one of the hirer's own
 * assigned pages — a channel item, a group tray member, or a panel button's
 * own `devices` (`hirerDevices.ts`).
 *
 * Every assigned page's detail is fetched here (not only the one currently
 * shown) so a device behind a tab the hirer has not yet opened still shows
 * the banner — `usePage`'s query key is shared with `PageSurface`'s own use
 * of it, so this adds no extra request for whichever page is already open.
 */
import { useQueries } from "@tanstack/react-query";

import { api } from "@/api/client";
import { Banner } from "@/components/ui/Banner";
import type { LightingChannel } from "@/lighting/types";
import { DEVICE_LABELS, useDeviceStatus, type DeviceName } from "@/live/deviceStatus";
import { pageKeys } from "@/pagesurface/api";
import { channelsFromPage } from "@/pagesurface/pageChannels";
import { devicesBehindPages } from "@/pagesurface/hirerDevices";
import type { PageDetail, PageSummary } from "@/pagesurface/types";

function joinLabels(labels: readonly string[]): string {
  if (labels.length <= 1) return labels[0] ?? "";
  if (labels.length === 2) return `${labels[0]} and ${labels[1]}`;
  return `${labels.slice(0, -1).join(", ")} and ${labels[labels.length - 1]}`;
}

export interface HirerDeviceBannerProps {
  pages: readonly PageSummary[];
}

export function HirerDeviceBanner({ pages }: HirerDeviceBannerProps) {
  // A fixed set of named hooks, not one per array entry — DEVICE_ORDER's five
  // names are constant, so calling each directly keeps this within the rules
  // of hooks while still reading every device's live status.
  const knx = useDeviceStatus("knx");
  const dmx = useDeviceStatus("dmx");
  const mixer = useDeviceStatus("mixer");
  const projector = useDeviceStatus("projector");
  const hdmi = useDeviceStatus("hdmi");
  const statusByDevice: Readonly<Record<DeviceName, { status: string }>> = { knx, dmx, mixer, projector, hdmi };

  const pageDetails = useQueries({
    queries: pages.map((page) => ({
      queryKey: pageKeys.detail(page.id),
      queryFn: () => api<PageDetail>(`/pages/${page.id}`),
    })),
  });

  const loadedPages = pageDetails.map((result) => result.data).filter((detail): detail is PageDetail => detail !== undefined);
  // A hirer may not read `GET /lighting/channels` (§16.5): the channel
  // objects the assigned pages themselves carry are what can be known.
  const channelsById = new Map<number, LightingChannel>(loadedPages.flatMap((detail) => [...channelsFromPage(detail.items)]));
  const relevant = devicesBehindPages(loadedPages, channelsById);

  const offline = Array.from(relevant).filter((name) => statusByDevice[name].status === "error");
  if (offline.length === 0) return null;

  const labels = offline.map((name) => DEVICE_LABELS[name]);
  return (
    <Banner tone="warning" title={`${joinLabels(labels)} ${offline.length === 1 ? "is" : "are"} currently unavailable`}>
      Please speak to venue staff if this affects your session.
    </Banner>
  );
}
