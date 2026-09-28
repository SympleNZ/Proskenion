/*
 * The content behind the `?` sheet (spec §21.24 *Help*, §24.2), reachable
 * from anywhere in all three shells (`shells/Shell.tsx` mounts `HelpSheet`
 * once for admin, operator and hirer alike) and, for admin only, also as the
 * System → Help screen reached from the sidebar — the same content either
 * way, spec §21.24's "Help" is one thing, not two.
 *
 * Content is per tier (§21.24, the 26 Sep milestone audit's finding that the
 * sheet existed only in the admin shell):
 *  - hirer: their shortcuts and a short "who to ask" line — never the
 *    recovery summary or the admin/operator documentation, which name
 *    internal contacts and procedures a hirer has no reason to see;
 *  - operator: shortcuts, the operator quick reference (`help/docs/`) and
 *    the version — no recovery summary, which is an admin's job;
 *  - admin: every shortcut, the version and build ID, the recovery summary,
 *    and all four bundled documents.
 *
 * "On this screen" (Simon's 28 Sep request) sits above all of that: it is
 * specific to wherever the sheet was opened from, where the rest of this is
 * the same on every screen. `docTarget` is the one piece of state this level
 * owns — `OnScreenSection`'s own "Read more" link sets it, `DocsSection`
 * consumes it to jump straight to a document and heading, and clears it once
 * handled so setting the same target again still fires.
 */
import { Fragment, useState } from "react";

import type { Tier } from "@/api/auth";
import { BUILD_ID, BUILD_VERSION } from "@/version/buildVersion";

import { DocsSection } from "./docs/DocsSection";
import type { DocId } from "./docs/docs";
import { OnScreenSection } from "./OnScreenSection";
import { shortcutsForTier } from "./shortcuts";

export interface HelpContentProps {
  tier: Tier;
}

export function HelpContent({ tier }: HelpContentProps) {
  const [docTarget, setDocTarget] = useState<{ id: DocId; heading: string } | null>(null);

  return (
    <div className="help-content">
      <OnScreenSection tier={tier} onOpenDoc={(id, heading) => setDocTarget({ id, heading })} />

      <section aria-labelledby="help-shortcuts-heading">
        <h2 className="sect-label" id="help-shortcuts-heading">
          Keyboard shortcuts
        </h2>
        <dl className="kv">
          {shortcutsForTier(tier).map((shortcut) => (
            <Fragment key={`${shortcut.keys}-${shortcut.description}`}>
              <dt className="technical">{shortcut.keys}</dt>
              <dd>
                {shortcut.description}
                {!shortcut.implemented ? (
                  <span className="pill" data-tone="warning">
                    Not built yet
                  </span>
                ) : null}
                {shortcut.note ? <p className="field-help">{shortcut.note}</p> : null}
              </dd>
            </Fragment>
          ))}
        </dl>
      </section>

      {tier === "hirer" ? (
        <section aria-labelledby="help-who-to-ask-heading">
          <h2 className="sect-label" id="help-who-to-ask-heading">
            Who to ask
          </h2>
          <p>Something not working? Please speak to venue staff.</p>
        </section>
      ) : (
        <>
          <section aria-labelledby="help-version-heading">
            <h2 className="sect-label" id="help-version-heading">
              Version
            </h2>
            <p className="technical">{BUILD_VERSION}</p>
            <p className="technical text-fg-muted text-sm">Build {BUILD_ID}</p>
          </section>

          <DocsSection tier={tier} openTarget={docTarget} onOpenTargetHandled={() => setDocTarget(null)} />
        </>
      )}

      {tier === "admin" ? (
        <section aria-labelledby="help-recovery-heading">
          <h2 className="sect-label" id="help-recovery-heading">
            If this appliance will not start
          </h2>
          <p>
            Insert the recovery USB stick — it boots to a console menu and a web interface on port 8080, with no SSH
            and no command line. If the SSD itself has failed and no stick is to hand, the CM5&apos;s own eMMC already
            carries the same recovery environment and boots automatically once the SSD is gone (§13.7). The laminated
            recovery card by the rack door has the full procedure and who to call.
          </p>
        </section>
      ) : null}
    </div>
  );
}
