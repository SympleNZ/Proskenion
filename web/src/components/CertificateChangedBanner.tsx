/*
 * The warning shown the moment a response says the controller's certificate
 * was just replaced — before the user's next action, which the browser would
 * otherwise refuse at TLS with nothing but "Could not reach the controller".
 *
 * On a host the new certificate names, reloading lets the browser show its
 * own prompt for the new certificate. On one it does not name (the bare IP,
 * once a restore has brought the real certificate back) no prompt will ever
 * accept it, so the banner says which address to use instead.
 */
import { CERTIFICATE_CHANGED_MESSAGE, hostCovered, preferredName, type CertificateChange } from "@/api/certificateChange";
import { Banner } from "@/components/ui/Banner";
import { Button } from "@/components/ui/Button";

export interface CertificateChangedBannerProps {
  change: CertificateChange;
  /** The page's host. Defaults to `window.location.hostname`. */
  host?: string;
  /** Defaults to reloading the page. */
  onReload?: () => void;
}

function currentHost(): string {
  return typeof window === "undefined" ? "" : window.location.hostname;
}

function reload(): void {
  window.location.reload();
}

export function CertificateChangedBanner({ change, host = currentHost(), onReload = reload }: CertificateChangedBannerProps) {
  const covered = change.names.length === 0 || hostCovered(host, change.names);
  const name = preferredName(change.names);
  if (!covered && name) {
    const target = `https://${name}/`;
    return (
      <Banner
        tone="warning"
        title="The controller's certificate has changed"
        action={
          <a className="btn btn-secondary" href={target}>
            Open {name}
          </a>
        }
      >
        This page is open at {host}, which the new certificate does not cover, so the browser cannot accept it here. Use{" "}
        <strong className="technical">{target}</strong> instead.
      </Banner>
    );
  }
  return (
    <Banner
      tone="warning"
      title="The controller's certificate has changed"
      action={
        <Button variant="secondary" onClick={onReload}>
          Reload
        </Button>
      }
    >
      {CERTIFICATE_CHANGED_MESSAGE}
    </Banner>
  );
}
