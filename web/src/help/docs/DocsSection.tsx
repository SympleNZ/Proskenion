/*
 * The `?` sheet's Documentation section (spec §21.24 *Help*, Simon's 26 Sep
 * decision to serve it from the controller itself). An operator sees only
 * the quick reference and it renders directly, with nothing to pick between;
 * an admin sees all four and picks one from a list first. Either way,
 * `RenderedMarkdown`'s `onNavigate` lets a link inside one doc switch
 * straight to another bundled one without leaving the sheet.
 *
 * `openTarget` is set from outside (the "On this screen" section's own "Read
 * more" link, `OnScreenSection.tsx`, via `HelpContent.tsx`): it opens the
 * named document and scrolls to the named heading, found by its rendered
 * text rather than a slug — the markdown renderer gives no heading an `id`,
 * and adding one for this alone was more machinery than four short,
 * hand-written documents need. `openId` is adjusted for a new `openTarget`
 * during render, not from an effect (React's own "adjusting state when a
 * prop changes" pattern, since `openTarget` is a plain prop, not an
 * external system) — `appliedTarget` is only there to notice that it
 * changed, by identity, since the same doc and heading can be asked for
 * twice in a row.
 */
import { useEffect, useRef, useState } from "react";
import { ArrowLeft } from "lucide-react";

import type { Tier } from "@/api/auth";
import { Button } from "@/components/ui/Button";

import { docsForTier, getDoc, type DocId } from "./docs";
import { RenderedMarkdown } from "./markdown";

export interface DocsSectionProps {
  tier: Tier;
  openTarget?: { id: DocId; heading: string } | null;
  /** Called once `openTarget` has been opened (and scrolled to, if found) — clears it in the caller so setting the same target again still fires. */
  onOpenTargetHandled?: () => void;
}

export function DocsSection({ tier, openTarget, onOpenTargetHandled }: DocsSectionProps) {
  const docs = docsForTier(tier);
  const [openId, setOpenId] = useState<DocId | null>(docs.length === 1 ? (docs[0]?.id ?? null) : null);
  const [appliedTarget, setAppliedTarget] = useState<DocsSectionProps["openTarget"]>(null);
  const readerRef = useRef<HTMLDivElement | null>(null);

  if (openTarget && openTarget !== appliedTarget) {
    setAppliedTarget(openTarget);
    setOpenId(openTarget.id);
  }

  useEffect(() => {
    if (!openTarget || openId !== openTarget.id) return;
    const container = readerRef.current;
    const heading = container
      ? Array.from(container.querySelectorAll("h1, h2, h3, h4, h5, h6")).find(
          (candidate) => candidate.textContent?.trim() === openTarget.heading,
        )
      : undefined;
    if (heading && typeof heading.scrollIntoView === "function") heading.scrollIntoView({ block: "start" });
    onOpenTargetHandled?.();
  }, [openTarget, openId, onOpenTargetHandled]);

  if (docs.length === 0) return null;

  const open = openId ? getDoc(openId) : null;

  return (
    <section aria-labelledby="help-docs-heading">
      <h2 className="sect-label" id="help-docs-heading">
        Documentation
      </h2>
      {open ? (
        <div className="doc-reader" ref={readerRef}>
          {docs.length > 1 ? (
            <Button variant="ghost" onClick={() => setOpenId(null)}>
              <ArrowLeft aria-hidden="true" className="size-4" />
              Back to Documentation
            </Button>
          ) : null}
          <h3 className="card-title">{open.title}</h3>
          <RenderedMarkdown source={open.raw} onNavigate={setOpenId} />
        </div>
      ) : (
        <ul className="doc-list">
          {docs.map((doc) => (
            <li key={doc.id}>
              <Button variant="secondary" block onClick={() => setOpenId(doc.id)}>
                {doc.title}
              </Button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
