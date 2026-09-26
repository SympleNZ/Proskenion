/*
 * Admin → Network (spec §21.24 *Network*, §10.8, contracts §5). The card is
 * one form — current values pre-fill it — with the Q5 apply flow: explain
 * before applying that the browser is about to be sent elsewhere, hand over
 * to the appliance's own `/reconnect` page after a `202`, and on return
 * confirm or report what happened.
 */
import { useEffect, useState } from "react";
import { Plus, Trash2 } from "lucide-react";

import { ApiError } from "@/api/client";
import { presentError, presentationFor } from "@/api/errors";
import { Banner } from "@/components/ui/Banner";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { ConfirmDialog } from "@/components/ui/Sheet";
import { ErrorState, Skeleton } from "@/components/ui/EmptyState";
import { Field, Input } from "@/components/ui/Input";
import { HelpButton } from "@/help/HelpButton";
import { formatTime } from "@/lib/time";

import { useApplyNetwork, useConfirmNetwork, useNetworkConfig, useNetworkState } from "./api";
import {
  clearArrivalToken,
  clearStoredPendingChange,
  readArrivalToken,
  readStoredPendingChange,
  reconnectUrl,
  storePendingChange,
  type StoredPendingChange,
} from "./reconnect";
import type { NetworkConfig } from "./types";
import { isNetworkFormValid, validateNetworkForm, type NetworkFormErrors, type NetworkFormValues } from "./validation";

function statusLine(error: unknown): string | undefined {
  return error instanceof ApiError ? `${error.status} ${error.code}` : undefined;
}

function formValuesFrom(config: NetworkConfig | undefined): NetworkFormValues {
  return {
    hostname: config?.hostname ?? "",
    address: config?.address ?? "",
    prefixLength: config?.prefix_length != null ? String(config.prefix_length) : "24",
    gateway: config?.gateway ?? "",
    dns: config && config.dns.length > 0 ? config.dns : [""],
  };
}

function formatClock(iso: string | null): string {
  if (!iso) return "";
  // Pacific/Auckland, 24-hour (CONVENTIONS.md), regardless of the viewing device's own zone.
  return formatTime(iso);
}

/** Merge the server's `validation_failed` field errors into the same slots the client-side check uses. */
function serverFieldErrors(error: unknown): NetworkFormErrors {
  if (!(error instanceof ApiError) || error.code !== "validation_failed") return {};
  const presentation = presentationFor(error);
  if (presentation.kind !== "inline") return {};
  const out: NetworkFormErrors = {};
  for (const [field, message] of Object.entries(presentation.fields)) {
    if (field === "hostname" || field === "address" || field === "prefix_length" || field === "gateway" || field === "dns") {
      out[field] = message;
    }
  }
  return out;
}

export function NetworkScreen() {
  const configQuery = useNetworkConfig();
  const apply = useApplyNetwork();
  const confirm = useConfirmNetwork();

  const [values, setValues] = useState<NetworkFormValues>(() => formValuesFrom(undefined));
  const [syncedAddress, setSyncedAddress] = useState<string | null | undefined>(undefined);
  const [errors, setErrors] = useState<NetworkFormErrors>({});
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [applyError, setApplyError] = useState<string | undefined>();
  // Lazy init: a plain one-time read, so this needs no effect (`useState`'s
  // initialiser argument runs exactly once, on mount, same as an effect with
  // an empty dependency array would, without the "setState in an effect"
  // cascading-render cost).
  const [stored, setStored] = useState<StoredPendingChange | null>(() => readStoredPendingChange(window.sessionStorage));
  // The token App.tsx captured off the URL fragment on arrival (reconnect.ts's
  // module doc): unlike `stored` above, this works from a different origin
  // too, because it needs no locally-remembered metadata to match against —
  // the server is the one authority on whether it matches the pending change.
  const [arrivalToken, setArrivalToken] = useState<string | null>(() => readArrivalToken(window.sessionStorage));
  const [arrivalError, setArrivalError] = useState<string | undefined>();
  const [outcome, setOutcome] = useState<{ kind: "confirmed" | "reverted"; address: string } | null>(null);

  const config = configQuery.data;

  // Confirm automatically: arriving here with this token already says the
  // new address answered (contracts §5 step 2's poll succeeded) — the
  // session-storage path above stays a manual "Confirm this address" for the
  // case where nothing so definite is known. A refused token (already used,
  // or belonging to a change that already reverted) is reported plainly
  // rather than retried.
  useEffect(() => {
    if (!arrivalToken) return undefined;
    let cancelled = false;
    confirm.mutate(arrivalToken, {
      onSuccess: () => {
        clearArrivalToken(window.sessionStorage);
        if (cancelled) return;
        setArrivalToken(null);
        void configQuery.refetch().then((fresh) => {
          if (!cancelled) setOutcome({ kind: "confirmed", address: fresh.data?.address ?? "the new address" });
        });
      },
      onError: (error) => {
        clearArrivalToken(window.sessionStorage);
        if (cancelled) return;
        setArrivalToken(null);
        if (error instanceof ApiError && error.code === "not_found") {
          setArrivalError("This confirmation link is no longer valid — the change may already be confirmed, or have reverted.");
        } else {
          presentError(error);
          setArrivalError("Could not confirm this address.");
        }
      },
    });
    return () => {
      cancelled = true;
    };
    // confirm/configQuery are hook results, not stable identities across
    // renders; this must run exactly once per distinct token, not on every
    // render either one happens to produce a new object.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [arrivalToken]);

  // Re-derive the form only when the loaded address changes, so editing is
  // never wiped out by a background refetch (the HirerAccessScreen pattern).
  if (config && config.address !== syncedAddress) {
    setSyncedAddress(config.address);
    setValues(formValuesFrom(config));
  }

  const networkState = useNetworkState();

  // The outcome of a change this browser initiated (reconnect.ts's module
  // doc explains why this is a best-effort, same-origin-only signal): once
  // the marker is no longer pending, the address either matches what was
  // applied (confirmed — by an admin, or automatically by the reconnect
  // page reaching it) or it does not (reverted, unattended, after 3 minutes).
  // Derived during render rather than in an effect (the same pattern as the
  // form re-sync above) — clearing `stored` here is what stops this from
  // running again on the next render.
  if (stored && config && networkState.data !== undefined && !networkState.data.pending) {
    const kind = config.address === stored.address ? "confirmed" : "reverted";
    clearStoredPendingChange(window.sessionStorage);
    setOutcome({ kind, address: config.address ?? stored.address });
    setStored(null);
  }

  if (configQuery.isPending) {
    return (
      <div className="view network-view" aria-busy="true" aria-label="Loading the network configuration">
        <h1 className="view-title">Network</h1>
        <Skeleton className="h-touch w-full" />
        <Skeleton className="h-touch w-full" />
      </div>
    );
  }

  if (configQuery.isError || !config) {
    return (
      <div className="view network-view">
        <h1 className="view-title">Network</h1>
        <ErrorState
          title="Could not load the network configuration"
          status={statusLine(configQuery.error)}
          onRetry={() => void configQuery.refetch()}
        />
      </div>
    );
  }

  const state = networkState.data;
  const isPending = state?.pending ?? false;
  const canConfirmNow = isPending && stored !== null && state?.reverts_at === stored.revertsAt;

  function setField<K extends keyof NetworkFormValues>(key: K, value: NetworkFormValues[K]): void {
    setValues((prev) => ({ ...prev, [key]: value }));
  }

  function setDns(index: number, value: string): void {
    setValues((prev) => ({ ...prev, dns: prev.dns.map((entry, i) => (i === index ? value : entry)) }));
  }

  function addDns(): void {
    setValues((prev) => (prev.dns.length >= 4 ? prev : { ...prev, dns: [...prev.dns, ""] }));
  }

  function removeDns(index: number): void {
    setValues((prev) => (prev.dns.length <= 1 ? prev : { ...prev, dns: prev.dns.filter((_, i) => i !== index) }));
  }

  function onApplyClick(): void {
    const validation = validateNetworkForm(values);
    setErrors(validation);
    setApplyError(undefined);
    if (!isNetworkFormValid(validation)) return;
    setConfirmOpen(true);
  }

  function doApply(): void {
    setConfirmOpen(false);
    const dns = values.dns.map((entry) => entry.trim()).filter(Boolean);
    apply.mutate(
      {
        hostname: values.hostname.trim().toLowerCase(),
        address: values.address.trim(),
        prefix_length: Number(values.prefixLength),
        gateway: values.gateway.trim(),
        dns,
      },
      {
        onSuccess: (result) => {
          storePendingChange(window.sessionStorage, {
            confirmToken: result.confirm_token,
            appliedAt: result.applied_at,
            revertsAt: result.reverts_at,
            address: result.address,
            hostname: result.hostname,
          });
          // §10.8: the browser is sent to the old address's own /reconnect
          // page, served on port 80 from the root image, not this SPA.
          window.location.href = reconnectUrl(window.location.host, result);
        },
        onError: (error) => {
          const fieldErrors = serverFieldErrors(error);
          if (Object.keys(fieldErrors).length > 0) {
            setErrors(fieldErrors);
            return;
          }
          if (error instanceof ApiError) {
            setApplyError(error.message);
            return;
          }
          presentError(error);
        },
      },
    );
  }

  function onConfirmClick(): void {
    if (!stored) return;
    confirm.mutate(stored.confirmToken, {
      onSuccess: () => {
        clearStoredPendingChange(window.sessionStorage);
        setStored(null);
        setOutcome({ kind: "confirmed", address: stored.address });
      },
      onError: (error) => presentError(error),
    });
  }

  return (
    <div className="view network-view">
      <header className="view-head">
        <div>
          <h1 className="view-title">Network</h1>
          <p className="view-lede">
            Currently {config.address ? `${config.address}/${config.prefix_length ?? "?"}` : "not configured"}
            {config.hostname ? ` · ${config.hostname}` : ""}.
          </p>
        </div>
      </header>

      {arrivalToken ? (
        <Banner tone="info" title="Confirming this address…">
          Reached by a reconnection link — confirming automatically.
        </Banner>
      ) : null}

      {arrivalError ? <Banner tone="warning">{arrivalError}</Banner> : null}

      {isPending ? (
        <Banner tone="warning" title={`This reverts at ${formatClock(state?.reverts_at ?? null)} unless confirmed`}>
          {state?.previous_address ? (
            <>Without confirmation, the previous address ({state.previous_address}) comes back automatically.</>
          ) : (
            <>Without confirmation, the previous settings come back automatically.</>
          )}
          {canConfirmNow ? (
            <div className="mt-2">
              <Button variant="primary" helpId="network.confirm" loading={confirm.isPending} onClick={onConfirmClick}>
                Confirm this address
              </Button>
            </div>
          ) : null}
        </Banner>
      ) : null}

      {outcome ? (
        <Banner tone={outcome.kind === "confirmed" ? "success" : "warning"}>
          {outcome.kind === "confirmed"
            ? `Confirmed — this controller is reachable at ${outcome.address}.`
            : `The change was not confirmed in time and was reverted. The address is now ${outcome.address}.`}
        </Banner>
      ) : null}

      <Card className="device-card" title="Address" titleLevel="h2">
        <Field label="Hostname" htmlFor="network-hostname" helpId="network.hostname" error={errors.hostname} errorId="network-hostname-error">
          <Input
            id="network-hostname"
            value={values.hostname}
            onChange={(e) => setField("hostname", e.currentTarget.value)}
            aria-invalid={errors.hostname ? true : undefined}
          />
        </Field>
        <div className="grid grid-cols-2 gap-4">
          <Field label="IP address" htmlFor="network-address" helpId="network.address" error={errors.address} errorId="network-address-error">
            <Input
              id="network-address"
              mono
              value={values.address}
              onChange={(e) => setField("address", e.currentTarget.value)}
              aria-invalid={errors.address ? true : undefined}
            />
          </Field>
          <Field label="Mask (prefix length)" htmlFor="network-prefix" helpId="network.prefix" error={errors.prefix_length} errorId="network-prefix-error">
            <Input
              id="network-prefix"
              type="number"
              min={0}
              max={32}
              mono
              value={values.prefixLength}
              onChange={(e) => setField("prefixLength", e.currentTarget.value)}
              aria-invalid={errors.prefix_length ? true : undefined}
            />
          </Field>
        </div>
        <Field label="Gateway" htmlFor="network-gateway" helpId="network.gateway" error={errors.gateway} errorId="network-gateway-error">
          <Input
            id="network-gateway"
            mono
            value={values.gateway}
            onChange={(e) => setField("gateway", e.currentTarget.value)}
            aria-invalid={errors.gateway ? true : undefined}
          />
        </Field>

        <div className="field">
          <div className="field-label-row">
            <span className="field-label" id="network-dns-label">
              DNS servers
            </span>
            <HelpButton id="network.dns" />
          </div>
          <div className="flex flex-col gap-2">
            {values.dns.map((entry, index) => (
              // Index as key: a bare list of address fields, reordered only by add/remove.
              <div key={index} className="flex gap-2">
                <Input
                  mono
                  aria-label={`DNS server ${index + 1}`}
                  value={entry}
                  onChange={(e) => setDns(index, e.currentTarget.value)}
                />
                <Button
                  variant="ghost"
                  size="icon"
                  aria-label={`Remove DNS server ${index + 1}`}
                  disabled={values.dns.length <= 1}
                  onClick={() => removeDns(index)}
                >
                  <Trash2 aria-hidden="true" className="size-4" />
                </Button>
              </div>
            ))}
          </div>
          <div className="field-error" id="network-dns-error" role="alert" aria-live="assertive">
            {errors.dns ?? ""}
          </div>
          <Button variant="secondary" size="standard" disabled={values.dns.length >= 4} onClick={addDns}>
            <Plus aria-hidden="true" className="size-4" />
            Add DNS server
          </Button>
        </div>

        {applyError ? (
          <p className="field-note" role="alert">
            {applyError}
          </p>
        ) : null}

        <div className="flex justify-end">
          <Button variant="primary" helpId="network.apply" loading={apply.isPending} onClick={onApplyClick}>
            Apply
          </Button>
        </div>
      </Card>

      <ConfirmDialog
        open={confirmOpen}
        onOpenChange={setConfirmOpen}
        title="Apply this network change?"
        description={
          <>
            Every connected staff and hirer session will drop the instant this applies. This browser will be sent to{" "}
            <code className="technical">{values.address || "the new address"}</code> to check the change took, and it
            reverts automatically in three minutes if nothing confirms it.
          </>
        }
        confirmLabel="Apply and reconnect"
        onConfirm={doApply}
      />
    </div>
  );
}
