/*
 * The `?` help sheet (spec §21.24 *Help*: "Reachable from anywhere with
 * ?", §24.2). Mounted once, in `shells/Shell.tsx`, so it is available from
 * every screen in all three shells — admin, operator and hirer alike —
 * without each one wiring up its own listener. Before 26 Sep 2026 this was
 * mounted only in `AdminShell`; the 26 Sep milestone audit found the
 * operator and hirer shells had no `?` sheet at all.
 *
 * A proper dialog: `ui/Sheet`'s `SheetContent` is Radix `Dialog.Content`,
 * which traps focus and closes on Escape (§24.3). Radix's own default
 * close-focus behaviour specifically refocuses `Dialog.Trigger` — there is
 * none here, since `?` is the trigger, not a click — so this remembers
 * whatever was focused when it opened and restores that itself.
 */
import { useEffect, useRef, useState } from "react";

import type { Tier } from "@/api/auth";
import { Sheet, SheetContent } from "@/components/ui/Sheet";

import { HelpContent } from "./HelpContent";

function isTyping(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  if (target.tagName === "INPUT" || target.tagName === "TEXTAREA") return true;
  // `isContentEditable` needs layout jsdom does not compute, so the
  // attribute itself is checked too — true in a real browser either way.
  if (target.isContentEditable) return true;
  const editable = target.closest("[contenteditable]");
  return editable !== null && editable.getAttribute("contenteditable") !== "false";
}

const DESCRIPTION_BY_TIER: Record<Tier, string> = {
  admin: "Keyboard shortcuts, this build's version, the bundled documentation, and what to do if the appliance will not start.",
  operator: "Keyboard shortcuts, the operator quick reference, and this build's version.",
  hirer: "Keyboard shortcuts, and who to ask if something isn't working.",
};

export function HelpSheet({ tier }: { tier: Tier }) {
  const [open, setOpen] = useState(false);
  const restoreFocusTo = useRef<HTMLElement | null>(null);

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent): void {
      if (event.key !== "?" || event.ctrlKey || event.metaKey || event.altKey) return;
      if (isTyping(event.target)) return;
      event.preventDefault();
      restoreFocusTo.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
      setOpen(true);
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, []);

  return (
    <Sheet open={open} onOpenChange={setOpen}>
      <SheetContent
        title="Help"
        description={DESCRIPTION_BY_TIER[tier]}
        onCloseAutoFocus={(event) => {
          event.preventDefault();
          restoreFocusTo.current?.focus();
        }}
      >
        <HelpContent tier={tier} />
      </SheetContent>
    </Sheet>
  );
}
