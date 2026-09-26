/*
 * "A new version is installed — Refresh" (spec §16 "Service worker"): shown
 * once `versionCheck.ts` finds the server running a build newer than this
 * one on screen. Never an auto-reload underneath someone — a banner with a
 * button, exactly like `CertificateChangedBanner`, and persistent the same
 * way (spec §21.26): it does not go away on its own, only on reload.
 */
import { Banner } from "@/components/ui/Banner";
import { Button } from "@/components/ui/Button";
import { applyUpdate } from "@/pwa/serviceWorker";
import { useNewVersion } from "@/version/versionCheck";

/** Activates the waiting service worker, then reloads (`applyUpdate`). */
function reload(): void {
  void applyUpdate();
}

export interface NewVersionBannerProps {
  /** Defaults to activating the new service worker and reloading the page. */
  onReload?: () => void;
}

export function NewVersionBanner({ onReload = reload }: NewVersionBannerProps) {
  const version = useNewVersion();
  if (version === null) return null;
  return (
    <Banner
      tone="info"
      title="A new version is installed"
      action={
        <Button variant="secondary" onClick={onReload}>
          Refresh
        </Button>
      }
    >
      Refresh this page to load it. Nothing you are doing will be interrupted until you do.
    </Banner>
  );
}
