/*
 * The portrait-only guard (the owner's decision, 2026-09 — see
 * `lib/phoneLandscape.ts`'s own doc comment for the detection rule and why
 * it lives outside §21.9's own "Phone landscape" adaptation).
 *
 * Always mounted, on every route (`main.tsx`, above `<App />` rather than
 * inside a shell, so login, the setup wizard and the hirer surface are all
 * covered): visibility is a pure CSS media query (`.phone-landscape-guard`,
 * components.css), so rotating the phone is resolved before the next paint
 * rather than waiting on a React render — no flash of the blocked view.
 * `display: none` outside the query already drops it from the accessibility
 * tree; nothing here needs to toggle `aria-hidden` on itself.
 *
 * It traps nothing and offers nothing to focus — there is no way to
 * dismiss it, because there is nothing to do but rotate the phone back —
 * so it carries no buttons and no focus management of its own. The app
 * keeps running underneath: nothing here touches the WebSocket, the query
 * cache or any component state, and rotating back to portrait simply stops
 * the media query matching, which un-hides the app instantly.
 */
import { Smartphone } from "lucide-react";
import { useId } from "react";

export function PhoneLandscapeGuard() {
  const messageId = useId();
  return (
    <div className="phone-landscape-guard" role="alertdialog" aria-modal="true" aria-labelledby={messageId}>
      <div className="phone-landscape-guard-body">
        <Smartphone aria-hidden="true" className="phone-landscape-guard-icon size-16" strokeWidth={1.25} />
        <p id={messageId}>Turn your phone upright — Proskenion works in portrait on phones.</p>
      </div>
    </div>
  );
}
