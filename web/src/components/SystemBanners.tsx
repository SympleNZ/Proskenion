/*
 * Persistent banners (spec §21.26): "between the status bar and page
 * content, not dismissable until the condition clears. Priority red over
 * amber over info." `useBanners()` already returns them in that order
 * (`live/store.ts`'s `setBanner`); this only maps level to tone and renders
 * them. Generic on purpose — every banner key the server raises
 * (`cert_expiring`, `email_unconfigured`, `device_offline`,
 * `venue_default_missing` and the rest) arrives through the one `banner`
 * frame this already understands (`live/store.ts`'s `applyMessage`), with
 * the server's own text, so a new key needs no interface change.
 *
 * One key adds something: `devices_offline` ("2 devices offline — tap for
 * details", §21.26) carries a Details button that lists which devices,
 * read from the same live device status the status bar shows. A device the
 * server still counts as offline while it retries shows there as
 * "Connecting", which is what its indicator says too.
 *
 * Both `progress` and `banner` frames are staff-only (contracts §6) — a
 * hirer's socket never carries one — so mounting this in every shell is
 * harmless rather than something that needs its own tier check.
 */
import { useId, useState } from "react";

import { Button } from "@/components/ui/Button";
import { DEVICE_LABELS, DEVICE_ORDER, STATUS_LABELS, useDeviceStatus, type DeviceName } from "@/live/deviceStatus";
import type { BannerLevel } from "@/live/store";
import { useBanners } from "@/live/store";

import { Banner, type BannerTone } from "./ui/Banner";

const TONE_BY_LEVEL: Readonly<Record<BannerLevel, BannerTone>> = {
  red: "danger",
  amber: "warning",
  info: "info",
};

/** §21.26's "Multiple offline" banner: the one whose text asks for a tap. */
export const DEVICES_OFFLINE_KEY = "devices_offline";

function OfflineDevice({ name }: { name: DeviceName }) {
  const { status, detail } = useDeviceStatus(name);
  if (status !== "error" && status !== "connecting") return null;
  const reason = detail?.last_error;
  return (
    <li>
      {DEVICE_LABELS[name]}: {STATUS_LABELS[status]}
      {reason ? <span className="technical"> — {reason}</span> : null}
    </li>
  );
}

/** One device offline; `DEVICES_OFFLINE_KEY` is two or more. Both restate what `ConnectionAnnouncer` says, so neither is live (§24.3). */
export const DEVICE_OFFLINE_KEY = "device_offline";

function DevicesOfflineBanner({ tone, text }: { tone: BannerTone; text: string }) {
  const [open, setOpen] = useState(false);
  const listId = useId();
  return (
    <Banner
      tone={tone}
      live={false}
      action={
        <Button variant="ghost" aria-expanded={open} aria-controls={open ? listId : undefined} onClick={() => setOpen((was) => !was)}>
          {open ? "Hide details" : "Details"}
        </Button>
      }
    >
      {text}
      {open ? (
        <ul id={listId} aria-label="Offline devices">
          {DEVICE_ORDER.map((name) => (
            <OfflineDevice key={name} name={name} />
          ))}
        </ul>
      ) : null}
    </Banner>
  );
}

export function SystemBanners() {
  const banners = useBanners();
  if (banners.length === 0) return null;
  return (
    <div className="system-banners">
      {banners.map((banner) =>
        banner.key === DEVICES_OFFLINE_KEY ? (
          <DevicesOfflineBanner key={banner.key} tone={TONE_BY_LEVEL[banner.level]} text={banner.text} />
        ) : (
          <Banner key={banner.key} tone={TONE_BY_LEVEL[banner.level]} live={banner.key !== DEVICE_OFFLINE_KEY}>
            {banner.text}
          </Banner>
        ),
      )}
    </div>
  );
}
