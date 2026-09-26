/*
 * The one <Toaster/> the app mounts (spec §21.26 "Toasts"): bottom-right on
 * desktop, above the tab bar on mobile. Three visible at once; no variant is
 * exempt from eviction — the oldest is dropped when a fourth arrives,
 * regardless of variant (§21.27 "Toasts report events; persistent conditions
 * use banners").
 *
 * Sonner already reflows to full width below its own 600 px breakpoint
 * (its `position` stays "bottom-right"; only the horizontal offset changes,
 * which is standard mobile toast layout). What stays ours to set is the
 * *vertical* clearance: this app's status bar (§21.7, B67 — the one bar this
 * interface keeps at the bottom, since in-app navigation never sits there)
 * is exactly the "tab bar" the spec means, so `mobileOffset` lifts the toast
 * stack clear of it rather than letting a toast land underneath it.
 */
import { Toaster } from "sonner";

/** The status bar's own height (`--touch-min`) plus its hairline border and a little air. */
export const MOBILE_TOAST_BOTTOM_OFFSET = "calc(var(--touch-min) + var(--hairline) + var(--space-2))";

export function AppToaster() {
  return (
    <Toaster
      theme="dark"
      position="bottom-right"
      visibleToasts={3}
      closeButton
      richColors={false}
      mobileOffset={{ bottom: MOBILE_TOAST_BOTTOM_OFFSET }}
    />
  );
}
