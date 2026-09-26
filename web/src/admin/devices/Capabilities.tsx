/*
 * Capabilities as connected (spec §21.24, §5.5 *Capabilities are resolved at
 * connect*, B56).
 *
 * A driver whose optional connection failed reports fewer capabilities than
 * its class advertises, and that difference is the diagnosis, so it is
 * surfaced rather than hidden.
 */
import { Check, X } from "lucide-react";

import { Banner } from "@/components/ui/Banner";
import { Skeleton } from "@/components/ui/EmptyState";

import { booleanCapabilities, capabilityFacts, capabilityLabel, missingCapabilities } from "./capabilityView";

export interface CapabilitiesPanelProps {
  /** What the connection actually achieved, or the declared set when unconnected. */
  capabilities: Record<string, unknown> | undefined;
  asConnected: boolean;
  /** The class's declared maximum, from GET /drivers. */
  declared?: Record<string, unknown> | undefined;
  loading?: boolean;
  /** Why the set could not be read at all — the device is unreachable. */
  unavailable?: string | undefined;
}

export function CapabilitiesPanel({
  capabilities,
  asConnected,
  declared,
  loading = false,
  unavailable,
}: CapabilitiesPanelProps) {
  if (loading) {
    return (
      <div className="caps" aria-busy="true" aria-label="Reading capabilities">
        <Skeleton className="h-touch w-full" />
      </div>
    );
  }

  if (!capabilities) {
    return (
      <Banner tone="warning" title="Capabilities are not available">
        {unavailable ?? "The device has not connected, so what it can do is not known yet."}
      </Banner>
    );
  }

  const booleans = booleanCapabilities(capabilities);
  const missing = missingCapabilities(declared, capabilities);
  const facts = capabilityFacts(capabilities);

  return (
    <div className="caps-panel">
      <p className="caps-source" data-connected={asConnected}>
        {asConnected
          ? "As connected — what this device reported after connecting."
          : "As declared — the device is not connected, so this is the driver's maximum, not what it achieved."}
      </p>
      <ul className="caps">
        {booleans.map(([key, value]) => (
          <li className="cap" data-present={value} key={key}>
            {value ? (
              <Check aria-hidden="true" className="size-4" strokeWidth={3} />
            ) : (
              <X aria-hidden="true" className="size-4" strokeWidth={3} />
            )}
            <span>{capabilityLabel(key)}</span>
            <span className="sr-only">{value ? " — available" : " — not available"}</span>
          </li>
        ))}
      </ul>
      {facts.length ? <p className="caps-facts">{facts.join(" · ")}</p> : null}
      {missing.length ? (
        <Banner tone="warning" title="Fewer capabilities than this driver declares">
          {missing.join(", ")} {missing.length === 1 ? "is" : "are"} declared by the driver but not reported by this
          connection. An optional connection has probably failed — that is the reason, not a fault elsewhere.
        </Banner>
      ) : null}
    </div>
  );
}
