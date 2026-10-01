/*
 * The common frame (spec §21.6, §21.7): navigation at the top, content in the
 * middle, the status bar along the bottom. The status bar is always the last
 * child; nothing is ever placed below it.
 *
 * `HelpSheet` is mounted here rather than per-shell (spec §21.24 *Help*:
 * "Reachable from anywhere with ?") — before 26 Sep 2026 it lived only in
 * `AdminShell`, so the operator and hirer shells had no `?` sheet at all.
 * `Shell` already knows the tier, so the sheet's content follows it too.
 */
import type { ReactNode } from "react";
import { Outlet } from "react-router-dom";

import type { Tier } from "@/api/auth";
import { ConnectionAnnouncer } from "@/components/ConnectionAnnouncer";
import { ConnectionBanner } from "@/components/ConnectionBanner";
import { NewVersionBanner } from "@/components/NewVersionBanner";
import { StatusBar } from "@/components/statusbar/StatusBar";
import { SystemBanners } from "@/components/SystemBanners";
import { HelpSheet } from "@/help/HelpSheet";
import { useConnectionState } from "@/live/connection";
import { useApplyDisplayScale } from "@/lib/useDisplayScale";
import { cn } from "@/lib/utils";
import { useManifest, type ManifestKind } from "@/pwa/useManifest";

import { useRouteFocus } from "./useRouteFocus";

export interface ShellProps {
  tier: Tier;
  manifest: ManifestKind;
  /** The header row: brand and tab strip, or the admin mobile header. */
  header?: ReactNode;
  /** Left of the main area: the operator rail on a phone in landscape, the admin sidebar. */
  aside?: ReactNode;
  children?: ReactNode;
  className?: string;
}

export function Shell({ tier, manifest, header, aside, children, className }: ShellProps) {
  useManifest(manifest);
  // §24.7: after an in-app navigation, focus moves to the new screen's heading.
  useRouteFocus();
  // Display scale (§21.9) is app-wide for operator and admin, and absent for
  // the hirer (§21.7 "The hirer has one button") — applied at the root so
  // portalled sheets, menus and toasts scale with the shell.
  useApplyDisplayScale(tier !== "hirer");
  const connection = useConnectionState();
  return (
    <div className={cn("shell", connection === "reconnecting" && "reconnecting", className)} data-testid="shell">
      {/* §24.7's checklist: a skip link past the navigation (the admin
          sidebar or the operator tab strip) to the main content — every
          screen otherwise makes a keyboard user tab through the whole nav
          again on every navigation. Hidden until it is the focused element. */}
      <a href="#main" className="skip-link">
        Skip to main content
      </a>
      {/* The one live region for device connection changes (§24.3). */}
      <ConnectionAnnouncer />
      {header}
      <ConnectionBanner />
      <NewVersionBanner />
      <SystemBanners />
      <div className="shell-body">
        {aside}
        {/* `.shell-main` is `overflow: auto` (the fixed-height shell keeps the
            header and status bar pinned, §21.7), so a screen whose content
            overflows it — Health, Help — makes it a genuinely scrollable
            region. `tabIndex={0}` keeps it the skip link's target and adds it
            to the tab order, satisfying axe's `scrollable-region-focusable`
            (Safari needs an element focused to scroll it with the keyboard)
            on a screen with no focusable content of its own to land on first. */}
        <main className="shell-main" id="main" tabIndex={0}>
          {children ?? <Outlet />}
        </main>
      </div>
      <StatusBar tier={tier} />
      <HelpSheet tier={tier} />
    </div>
  );
}
