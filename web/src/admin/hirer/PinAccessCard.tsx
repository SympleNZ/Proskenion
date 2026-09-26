/*
 * The two post-hire actions (spec §6.6, §21.20): change the PIN, and the
 * instant kill switch. Both take effect the moment the request answers —
 * neither is part of the `PUT /hirer/config` save below, because the
 * contract (`proskenion/api/hirer.py`) commits each one on its own `POST`.
 */
import { useState } from "react";
import { Copy } from "lucide-react";
import { toast } from "sonner";

import { ApiError } from "@/api/client";
import { presentError } from "@/api/errors";
import { Banner } from "@/components/ui/Banner";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { Skeleton } from "@/components/ui/EmptyState";
import { Checkbox } from "@/components/ui/Select";
import { ConfirmDialog } from "@/components/ui/Sheet";
import { HelpButton } from "@/help/HelpButton";
import { PinInput } from "@/components/PinInput";

import { useHirerConfig, useSetHirerEnabled, useSetHirerPin } from "./api";
import { PLACEHOLDER_PIN_REASON } from "./types";

function sessionsClosedLine(count: number): string {
  return count === 1 ? "1 hirer session closed." : `${count} hirer sessions closed.`;
}

// A card-level help trigger, so the card is covered even while it is showing
// nothing but "Change PIN" and the access checkbox — the PIN form's own
// "Set PIN" help only exists once that form is open (spec §19.1; see
// `help/coverage.ts`'s card rule).
const CARD_TITLE = (
  <span className="flex items-center gap-2">
    Access and PIN
    <HelpButton id="hirer.access-and-pin" label="Access and PIN" />
  </span>
);

export function PinAccessCard() {
  const config = useHirerConfig();
  const setPin = useSetHirerPin();
  const setEnabled = useSetHirerEnabled();

  const [pinFormOpen, setPinFormOpen] = useState(false);
  const [pinDigits, setPinDigits] = useState("");
  const [pinError, setPinError] = useState<string | undefined>();
  const [revealed, setRevealed] = useState<{ pin: string | undefined; sessionsClosed: number } | null>(null);

  const [enabledError, setEnabledError] = useState<string | undefined>();
  const [enabledResult, setEnabledResult] = useState<{ enabled: boolean; sessionsClosed: number } | null>(null);
  const [confirmDisable, setConfirmDisable] = useState(false);

  if (config.isPending) {
    return (
      <Card className="device-card" title="Access and PIN" titleLevel="h2">
        <div aria-busy="true" aria-label="Loading hirer access">
          <Skeleton className="h-touch w-full" />
        </div>
      </Card>
    );
  }

  const data = config.data;
  if (config.isError || !data) {
    return (
      <Card className="device-card" title="Access and PIN" titleLevel="h2">
        <Banner tone="danger">Could not load the hirer PIN and access state.</Banner>
      </Card>
    );
  }

  function openPinForm(): void {
    setPinFormOpen(true);
    setPinDigits("");
    setPinError(undefined);
    setRevealed(null);
  }

  function submitDigits(): void {
    if (pinDigits.length !== 6 || !/^[0-9]{6}$/.test(pinDigits)) {
      setPinError("Enter exactly six digits");
      return;
    }
    setPinError(undefined);
    setPin.mutate(
      { pin: pinDigits },
      {
        onSuccess: (result) => {
          setPinFormOpen(false);
          setRevealed({ pin: undefined, sessionsClosed: result.sessions_closed });
          setEnabledError(undefined);
        },
        onError: (error) => presentError(error),
      },
    );
  }

  function submitGenerate(): void {
    setPinError(undefined);
    setPin.mutate(
      { generate: true },
      {
        onSuccess: (result) => {
          setPinFormOpen(false);
          setRevealed({ pin: result.pin, sessionsClosed: result.sessions_closed });
          setEnabledError(undefined);
        },
        onError: (error) => presentError(error),
      },
    );
  }

  function copyRevealedPin(): void {
    if (!revealed?.pin) return;
    navigator.clipboard
      ?.writeText(revealed.pin)
      .then(() => toast.success("PIN copied"))
      .catch(() => undefined);
  }

  function toggleEnabled(next: boolean): void {
    setEnabledError(undefined);
    if (!next) {
      setConfirmDisable(true);
      return;
    }
    setEnabled.mutate(true, {
      onSuccess: (result) => setEnabledResult({ enabled: result.enabled, sessionsClosed: result.sessions_closed }),
      onError: (error) => {
        if (error instanceof ApiError && error.code === "validation_failed" && error.reason === PLACEHOLDER_PIN_REASON) {
          setEnabledError(error.message);
          openPinForm();
          return;
        }
        presentError(error);
      },
    });
  }

  function confirmDisableAccess(): void {
    setConfirmDisable(false);
    setEnabled.mutate(false, {
      onSuccess: (result) => setEnabledResult({ enabled: result.enabled, sessionsClosed: result.sessions_closed }),
      onError: (error) => presentError(error),
    });
  }

  return (
    <Card className="device-card" title={CARD_TITLE} titleLevel="h2">
      <div className="flex flex-wrap items-center gap-3">
        <span className="field-label">Current PIN</span>
        {data.pin_is_placeholder ? (
          <span className="pill" data-tone="warning">
            Placeholder — not yet set
          </span>
        ) : (
          <span className="technical">••••••</span>
        )}
        <Button variant="secondary" onClick={() => (pinFormOpen ? setPinFormOpen(false) : openPinForm())}>
          Change PIN
        </Button>
      </div>

      {pinFormOpen ? (
        <div className="flex flex-col gap-3">
          <Banner tone="warning">Changing the PIN signs out every hirer connected right now.</Banner>
          <div className="flex flex-col gap-2">
            <PinInput
              label="New PIN"
              value={pinDigits}
              onChange={(value) => {
                setPinDigits(value);
                setPinError(undefined);
              }}
              onComplete={() => setPinError(undefined)}
            />
            {pinError ? (
              <p className="field-note" role="alert">
                {pinError}
              </p>
            ) : null}
            <div className="flex gap-3">
              <Button variant="primary" helpId="hirer.pin.set" loading={setPin.isPending} disabled={pinDigits.length !== 6} onClick={submitDigits}>
                Set PIN
              </Button>
              <Button variant="secondary" loading={setPin.isPending} onClick={submitGenerate}>
                Generate a PIN
              </Button>
              <Button variant="ghost" onClick={() => setPinFormOpen(false)}>
                Cancel
              </Button>
            </div>
          </div>
        </div>
      ) : null}

      {revealed ? (
        <Banner tone="success" title={revealed.pin ? "New PIN generated" : "PIN changed"}>
          {revealed.pin ? (
            <div className="flex flex-col gap-2">
              <div className="connection-row">
                <span className="technical text-2xl tracking-widest" data-testid="generated-pin">
                  {revealed.pin}
                </span>
                <Button variant="secondary" onClick={copyRevealedPin}>
                  <Copy aria-hidden="true" className="size-4" />
                  Copy
                </Button>
              </div>
              <p className="text-fg-muted text-sm">This PIN is shown once, here, and never again.</p>
            </div>
          ) : null}
          <p>{sessionsClosedLine(revealed.sessionsClosed)}</p>
        </Banner>
      ) : null}

      <div className="flex flex-wrap items-center gap-3">
        <span className="field-label">Access</span>
        <Checkbox
          id="hirer-access-enabled"
          label="Hire guest access enabled"
          checked={data.enabled}
          onChange={(event) => toggleEnabled(event.currentTarget.checked)}
        />
        <span className="field-help">Disabling drops active hirer connections immediately.</span>
      </div>

      {enabledError ? (
        <Banner tone="danger" title="Cannot enable access">
          {enabledError}
        </Banner>
      ) : null}

      {enabledResult ? (
        <Banner tone={enabledResult.enabled ? "success" : "info"}>
          {enabledResult.enabled ? "Access enabled." : "Access disabled."} {sessionsClosedLine(enabledResult.sessionsClosed)}
        </Banner>
      ) : null}

      <ConfirmDialog
        open={confirmDisable}
        onOpenChange={setConfirmDisable}
        title="Disable hirer access?"
        description="Disabling drops every connected hirer at once and blocks new sign-ins until it is turned back on."
        confirmLabel="Disable access"
        destructive
        onConfirm={confirmDisableAccess}
      />
    </Card>
  );
}
