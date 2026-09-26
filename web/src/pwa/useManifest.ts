/*
 * Two manifests, two installed identities (spec §10.2): manifest.staff.json
 * starts at /app and manifest.hirer.json at /hire. The shell picks the link
 * by route so an install from either surface lands back on it.
 *
 * The apple-touch-icon travels the same way (§21.28): iOS Safari reads it
 * straight from the DOM when someone taps Share → Add to Home Screen, and
 * both shells share the one `index.html`, so it is swapped per shell exactly
 * as the manifest link is, rather than left as one fixed icon for both.
 */
import { useEffect } from "react";

export type ManifestKind = "staff" | "hirer";

export const MANIFEST_HREF: Readonly<Record<ManifestKind, string>> = {
  staff: "/manifest.staff.json",
  hirer: "/manifest.hirer.json",
};

/** Distinct icons per shell (§21.28) — iOS ignores SVG here, so both are PNG. */
export const APPLE_TOUCH_ICON_HREF: Readonly<Record<ManifestKind, string>> = {
  staff: "/icons/apple-touch-icon.png",
  hirer: "/icons/hirer-apple-touch-icon.png",
};

function applyLink(id: string, rel: string, href: string): void {
  let link = document.getElementById(id) as HTMLLinkElement | null;
  if (!link) {
    link = document.createElement("link");
    link.id = id;
    link.rel = rel;
    document.head.appendChild(link);
  }
  if (link.getAttribute("href") !== href) link.setAttribute("href", href);
}

export function applyManifest(kind: ManifestKind): void {
  applyLink("app-manifest", "manifest", MANIFEST_HREF[kind]);
  applyLink("apple-touch-icon", "apple-touch-icon", APPLE_TOUCH_ICON_HREF[kind]);
}

export function useManifest(kind: ManifestKind): void {
  useEffect(() => {
    applyManifest(kind);
  }, [kind]);
}
