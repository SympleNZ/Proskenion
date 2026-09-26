/*
 * The `?` sheet's Documentation section (spec §21.24 *Help*, Simon's 26 Sep
 * decision to serve it from the controller itself). An operator sees only
 * the quick reference and it renders directly, with nothing to pick between;
 * an admin sees all four and picks one from a list first. Either way,
 * `RenderedMarkdown`'s `onNavigate` lets a link inside one doc switch
 * straight to another bundled one without leaving the sheet.
 */
import { useState } from "react";
import { ArrowLeft } from "lucide-react";

import type { Tier } from "@/api/auth";
import { Button } from "@/components/ui/Button";

import { docsForTier, getDoc, type DocId } from "./docs";
import { RenderedMarkdown } from "./markdown";

export interface DocsSectionProps {
  tier: Tier;
}

export function DocsSection({ tier }: DocsSectionProps) {
  const docs = docsForTier(tier);
  const [openId, setOpenId] = useState<DocId | null>(docs.length === 1 ? (docs[0]?.id ?? null) : null);

  if (docs.length === 0) return null;

  const open = openId ? getDoc(openId) : null;

  return (
    <section aria-labelledby="help-docs-heading">
      <h2 className="sect-label" id="help-docs-heading">
        Documentation
      </h2>
      {open ? (
        <div className="doc-reader">
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
