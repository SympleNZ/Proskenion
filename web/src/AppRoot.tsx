/*
 * The top of the tree, above `<App />` (mounted by `main.tsx`, on every
 * route — login, the setup wizard, both operator/admin shells and the hirer
 * surface, all under `<App />`'s own `<Routes>`).
 *
 * The portrait-only guard (`PhoneLandscapeGuard`, the owner's decision,
 * 2026-09 — see `lib/phoneLandscape.ts`'s own doc comment) sits beside the
 * app, not inside it: `#app-root` is `inert` and `aria-hidden` while a
 * phone is in landscape, which would swallow the guard's own `alertdialog`
 * too if it were a descendant. The guard's own visibility is CSS, not this
 * component re-rendering — `usePhoneLandscape` here only gates `inert`,
 * which CSS cannot set on its own, so a lagging render is harmless: the
 * view is already covered by the time assistive tech would reach it.
 *
 * Nothing here touches the WebSocket, the query cache or any route's own
 * state, so the app keeps running underneath the overlay exactly as it was;
 * rotating back to portrait only stops the media query matching and un-inerts
 * this element again.
 */
import type { ReactNode } from "react";

import { PhoneLandscapeGuard } from "./components/PhoneLandscapeGuard";
import { usePhoneLandscape } from "./lib/phoneLandscape";

export function AppRoot({ children }: { children: ReactNode }) {
  const phoneLandscape = usePhoneLandscape();
  return (
    <>
      <PhoneLandscapeGuard />
      <div id="app-root" inert={phoneLandscape} aria-hidden={phoneLandscape || undefined}>
        {children}
      </div>
    </>
  );
}
