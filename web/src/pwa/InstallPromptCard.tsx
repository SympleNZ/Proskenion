/*
 * The hirer install prompt (spec §21.8, §6.16, §18 Q10). Mounted by
 * `HirerShell` only — staff never see this. It shows after a successful
 * sign-in, once the session response has settled `certificate`
 * (`session/context.ts`), and decides which of the four cards to show — or
 * none — purely through `installPrompt.ts`'s pure `decideInstallPrompt`, so
 * the platform sniffing and the localStorage bookkeeping stay out of the
 * render branches themselves.
 */
import { useEffect, useState } from "react";

import { CertificateTrustNotice } from "@/components/CertificateTrustNotice";
import { Banner } from "@/components/ui/Banner";
import { Button } from "@/components/ui/Button";
import { useSession } from "@/session/context";

import {
  decideInstallPrompt,
  detectPlatform,
  hasShownIosInstructions,
  isNotNowActive,
  isStandalone,
  recordIosInstructionsShown,
  recordNotNow,
} from "./installPrompt";

/**
 * Chrome's own event, not yet in `lib.dom.d.ts` (still non-standard). Narrowed
 * from a plain `Event` by `hasPromptMethod` below rather than assumed.
 */
interface BeforeInstallPromptEvent extends Event {
  prompt(): Promise<void>;
  readonly userChoice: Promise<{ outcome: "accepted" | "dismissed"; platform: string }>;
}

function hasPromptMethod(event: Event): event is BeforeInstallPromptEvent {
  return typeof (event as Partial<BeforeInstallPromptEvent>).prompt === "function";
}

function matchesStandaloneDisplay(): boolean {
  return typeof window.matchMedia === "function" && window.matchMedia("(display-mode: standalone)").matches;
}

export function InstallPrompt() {
  const { session } = useSession();
  const [deferred, setDeferred] = useState<BeforeInstallPromptEvent | null>(null);
  // Acting on the Android card ("Add"/"Not now") or displaying the iOS one
  // both need to hide the card immediately, without waiting on a re-render
  // that reads localStorage back — this is that one extra "hide it now" bit.
  const [dismissed, setDismissed] = useState(false);

  useEffect(() => {
    function onBeforeInstallPrompt(event: Event): void {
      // Chrome's own default is a mini-infobar; §21.8 wants this card
      // offering the same action instead.
      event.preventDefault();
      if (hasPromptMethod(event)) setDeferred(event);
    }
    window.addEventListener("beforeinstallprompt", onBeforeInstallPrompt);
    return () => window.removeEventListener("beforeinstallprompt", onBeforeInstallPrompt);
  }, []);

  // Never shown to staff (§18) — checked here, not only by which
  // shell happens to mount this component, so it holds even if it were ever
  // reached from somewhere else.
  const staff = session !== null && session.tier !== "hirer";
  const platform = detectPlatform(navigator.userAgent, navigator.maxTouchPoints || 0);
  const decision =
    dismissed || staff
      ? ({ kind: "none" } as const)
      : decideInstallPrompt({
          certificate: session?.certificate ?? "trusted",
          platform,
          standalone: isStandalone((navigator as Navigator & { standalone?: boolean }).standalone, matchesStandaloneDisplay()),
          hasCapturedPrompt: deferred !== null,
          notNowActive: isNotNowActive(window.localStorage),
          iosAlreadyShown: hasShownIosInstructions(window.localStorage),
        });

  // "Shown once and never again" (§21.8) is the display itself, not waiting
  // for the "Got it" tap — someone who never dismisses it must not see it
  // resurface on their next visit either.
  useEffect(() => {
    if (decision.kind === "ios") recordIosInstructionsShown(window.localStorage);
  }, [decision.kind]);

  if (!session || decision.kind === "none") return null;

  if (decision.kind === "self_signed") {
    return (
      <Banner tone="info" title="This device can't be added to the Home Screen yet">
        <p>
          This venue's security certificate isn't trusted here. <CertificateTrustNotice /> Or carry on in the
          browser — everything else works the same way.
        </p>
      </Banner>
    );
  }

  if (decision.kind === "android") {
    return (
      <Banner
        tone="info"
        title="Add this to your Home Screen?"
        action={
          <div className="install-prompt-actions">
            <Button
              variant="primary"
              size="hirer"
              onClick={() => {
                void deferred?.prompt().then(() => setDeferred(null));
              }}
            >
              Add
            </Button>
            <Button
              variant="ghost"
              size="hirer"
              onClick={() => {
                recordNotNow(window.localStorage);
                setDismissed(true);
              }}
            >
              Not now
            </Button>
          </div>
        }
      >
        It opens like its own app, with no browser bar in the way.
      </Banner>
    );
  }

  return (
    <Banner
      tone="info"
      title="Add this to your Home Screen"
      action={
        <Button variant="ghost" size="hirer" onClick={() => setDismissed(true)}>
          Got it
        </Button>
      }
    >
      Tap Share, then &quot;Add to Home Screen&quot;.
    </Banner>
  );
}
