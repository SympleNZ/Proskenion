/*
 * Focus follows navigation (spec §24.7; found with Narrator + Edge on the
 * appliance, 1 Oct 2026 — docs/hardware/accessibility-check.md §1).
 *
 * Pressing Enter on a tab or sidebar link swaps the screen, but a client-side
 * router leaves focus on the link: a keyboard or screen-reader user is never
 * told the new screen loaded and has to Tab through the rest of the
 * navigation to reach it. After an in-app navigation this moves focus to the
 * new screen's `h1` (every screen has one, a visually hidden one where the
 * design shows none; `tabindex="-1"` is added here, so the heading is
 * focusable without joining the tab order), which Narrator reads, and the
 * next Tab continues into the page.
 *
 * Implemented once, in `Shell`, keyed on the pathname — not per screen.
 *
 * When it does NOT move focus (the judgement calls):
 *   - the initial load, and a REPLACE navigation (a `<Navigate replace>`
 *     redirect such as /app -> /app/pages, a wrong hirer page id): nothing the
 *     user did, and the first Tab must still reach the skip link;
 *   - while a sheet or dialog is open (not the admin navigation drawer, which
 *     closes itself on navigation): its focus trap owns focus;
 *   - while focus is in a text field outside the navigation: the user is
 *     typing, so a route change from elsewhere (back/forward) must not pull
 *     the caret away.
 *
 * A screen's heading may not exist yet (it loads data first), so this waits
 * for one — up to `HEADING_WAIT_MS` — and falls back to `#main` itself, which
 * is focusable. If a dialog opens or the user starts typing meanwhile, it
 * gives up.
 */
import { useEffect, useRef } from "react";
import { useLocation, useNavigationType } from "react-router-dom";

export const HEADING_WAIT_MS = 1000;

const EDITABLE =
  'input:not([type="button"]):not([type="checkbox"]):not([type="radio"]):not([type="submit"]), textarea, select, [contenteditable=""], [contenteditable="true"]';
const NAVIGATION = "nav, header, aside";
const OPEN_DIALOG = '[role="dialog"][data-state="open"]:not([data-nav-drawer]), [role="alertdialog"][data-state="open"]';

function mayMoveFocus(): boolean {
  if (document.querySelector(OPEN_DIALOG) !== null) return false;
  const active = document.activeElement;
  const typing = active instanceof HTMLElement && active.matches(EDITABLE) && !active.closest(NAVIGATION);
  return !typing;
}

function focusTarget(target: HTMLElement): void {
  if (!target.hasAttribute("tabindex")) target.setAttribute("tabindex", "-1");
  target.focus({ preventScroll: true });
}

export function useRouteFocus(): void {
  const { pathname } = useLocation();
  const navigationType = useNavigationType();
  const previous = useRef(pathname);

  useEffect(() => {
    if (previous.current === pathname) return undefined;
    previous.current = pathname;
    if (navigationType === "REPLACE") return undefined;

    const main = document.getElementById("main");
    if (!main) return undefined;

    let done = false;
    const timers: number[] = [];
    const observer = new MutationObserver(() => attempt(false));
    function finish(): void {
      done = true;
      observer.disconnect();
      for (const timer of timers) window.clearTimeout(timer);
    }
    function attempt(final: boolean): void {
      if (done) return;
      if (!mayMoveFocus()) {
        finish();
        return;
      }
      const heading = main?.querySelector<HTMLElement>("h1");
      if (heading) {
        focusTarget(heading);
        finish();
      } else if (final && main) {
        focusTarget(main);
        finish();
      }
    }

    observer.observe(main, { childList: true, subtree: true });
    // After this render's own commit and any sheet's close-focus handling.
    timers.push(window.setTimeout(() => attempt(false), 0));
    timers.push(window.setTimeout(() => attempt(true), HEADING_WAIT_MS));
    return finish;
  }, [pathname, navigationType]);
}
