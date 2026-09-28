/*
 * The `?` sheet's "On this screen" section (spec §21.24 *Help*, Simon's 28
 * Sep request): what the currently open screen is for, every inline help
 * entry on it in the order the controls appear, and — where one exists — a
 * link into the matching section of the bundled documentation.
 *
 * Resolved from the route rather than passed in: `HelpSheet` is mounted once
 * per shell (`shells/Shell.tsx`) and knows nothing about which screen is
 * open beneath it, so this reads `useLocation()` itself. The entries come
 * from the rendered DOM, not a hand-maintained list — see `onScreen.ts`'s
 * module doc for why.
 */
import { Fragment, useLayoutEffect, useState } from "react";
import { useLocation } from "react-router-dom";

import type { Tier } from "@/api/auth";
import { Button } from "@/components/ui/Button";

import type { DocId } from "./docs/docs";
import { collectOnScreenEntries, resolveScreen, type OnScreenEntry } from "./onScreen";

export interface OnScreenSectionProps {
  tier: Tier;
  /** Opens the matching bundled doc, scrolled to the given heading (`HelpContent.tsx` wires this to `DocsSection`). */
  onOpenDoc: (id: DocId, heading: string) => void;
}

export function OnScreenSection({ tier, onOpenDoc }: OnScreenSectionProps) {
  const { pathname } = useLocation();
  const [entries, setEntries] = useState<readonly OnScreenEntry[]>([]);

  // `#main` (`shells/Shell.tsx`) is genuinely external to this component —
  // owned by a sibling part of the tree, not by this component's own props
  // or state — and reading it during render is unsafe for the one case
  // where this section's own screen (Admin → Help, `HelpScreen.tsx`) is
  // itself `#main`'s new content: mid-navigation, before commit, `#main`
  // still holds the *previous* screen's markup, and nothing would otherwise
  // force a second render to correct that. A layout effect runs after this
  // commit's DOM mutations land — including `#main`'s own — so it always
  // sees the screen that is actually on screen, whichever kind of update
  // this is.
  useLayoutEffect(() => {
    const root = document.getElementById("main") ?? document;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- synchronising with `#main`'s DOM, an external system this component does not own, not deriving state from a prop or from this component's own earlier state
    setEntries(collectOnScreenEntries(root));
  }, [pathname]);

  const resolved = resolveScreen(tier, pathname);
  if (!resolved) return null;
  const { help } = resolved;
  const doc = help.doc;

  return (
    <section aria-labelledby="help-on-screen-heading">
      <h2 className="sect-label" id="help-on-screen-heading">
        On this screen
      </h2>
      <p>{help.summary}</p>
      {entries.length > 0 ? (
        <dl className="kv">
          {entries.map((entry) => (
            <Fragment key={entry.id}>
              <dt>{entry.term}</dt>
              <dd>{entry.body}</dd>
            </Fragment>
          ))}
        </dl>
      ) : null}
      {doc ? (
        <Button variant="ghost" onClick={() => onOpenDoc(doc.id, doc.heading)}>
          Read more in the documentation
        </Button>
      ) : null}
    </section>
  );
}
