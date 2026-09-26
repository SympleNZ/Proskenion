/*
 * The action-type picker (spec §21.16): a tile per domain. An unavailable
 * domain is shown disabled with its reason rather than hidden — §5.5's rule
 * for capability gating applies to the picker as much as to a saved action.
 */
import { DOMAINS, DOMAIN_LABELS, type Domain, type DomainAvailability } from "./types";

export interface DomainPickerProps {
  availability: readonly DomainAvailability[] | undefined;
  selected: Domain | null;
  onSelect: (domain: Domain) => void;
}

export function DomainPicker({ availability, selected, onSelect }: DomainPickerProps) {
  const byDomain = new Map(availability?.map((row) => [row.domain, row] as const));
  return (
    <div className="domain-picker" role="group" aria-label="Action type">
      {DOMAINS.map((domain) => {
        const row = byDomain.get(domain);
        // Unknown until `/scenes/domains` answers: treated as unavailable
        // rather than clickable, so nothing can be picked out from under a
        // slow load.
        const available = row?.available ?? false;
        const reason = row?.reason ?? undefined;
        return (
          <button
            key={domain}
            type="button"
            className="domain-tile"
            aria-pressed={selected === domain}
            disabled={!available}
            title={reason}
            onClick={() => onSelect(domain)}
          >
            <span>{DOMAIN_LABELS[domain]}</span>
            {!available && reason ? <span className="text-fg-muted text-xs">{reason}</span> : null}
          </button>
        );
      })}
    </div>
  );
}
