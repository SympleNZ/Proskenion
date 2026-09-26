/*
 * The "Maps to" driver-reference picker (§21.21, §5.5 *Ganged channels*):
 * one or more of the driver's `available_refs()`, ordered — the first is
 * authoritative for display — picked from `GET /devices/{id}/refs` with
 * human labels, never typed free-hand. "+ Gang another" adds a second
 * reference the way §21.21's mock-up shows; removing back to zero returns
 * to the single "choose a reference" state a new channel starts in.
 */
import { X } from "lucide-react";

import { Button } from "@/components/ui/Button";
import { Select } from "@/components/ui/Select";
import { HelpButton } from "@/help/HelpButton";

import type { ChannelRef } from "./types";

/**
 * The option text a picker shows: the driver's human label plus its opaque
 * ref, because the audio engineer needs the ref to talk about the same
 * channel in MixPad (§5.5 "driver_ref is shown in configuration views").
 * A label that already carries its own parenthetical — "Out 1/2 (linked)" —
 * gets an em-dash instead of a second, nested set of parentheses.
 */
function refOptionLabel(ref: ChannelRef): string {
  return ref.label.includes("(") ? `${ref.label} — ${ref.ref}` : `${ref.label} (${ref.ref})`;
}

function refLabelFor(refs: readonly ChannelRef[], code: string): string {
  return refs.find((r) => r.ref === code)?.label ?? code;
}

export interface RefPickerProps {
  idPrefix: string;
  label?: string;
  /** Already restricted to the relevant kind ("input" or "output") by the caller. */
  refs: readonly ChannelRef[];
  /** Ordered driver refs; the first is authoritative for display (§5.5). */
  value: readonly string[];
  onChange: (next: string[]) => void;
  disabled?: boolean;
}

export function RefPicker({ idPrefix, label = "Maps to", refs, value, onChange, disabled = false }: RefPickerProps) {
  const used = new Set(value);
  const canAddAnother = refs.some((r) => !used.has(r.ref));

  function setAt(index: number, ref: string): void {
    const next = [...value];
    next[index] = ref;
    onChange(next);
  }

  function removeAt(index: number): void {
    onChange(value.filter((_, i) => i !== index));
  }

  function addAnother(): void {
    const next = refs.find((r) => !used.has(r.ref));
    if (next) onChange([...value, next.ref]);
  }

  function chooseFirst(ref: string): void {
    onChange(ref ? [ref] : []);
  }

  return (
    <fieldset className="field schema-field" disabled={disabled}>
      <legend className="field-label">{label}</legend>
      <HelpButton id="mixer.channel.refs" />
      {refs.length === 0 ? (
        <p className="metric-note">No references available from this driver.</p>
      ) : value.length === 0 ? (
        <Select id={`${idPrefix}-ref-0`} aria-label={label} value="" onChange={(event) => chooseFirst(event.currentTarget.value)}>
          <option value="">Choose a reference</option>
          {refs.map((ref) => (
            <option key={ref.ref} value={ref.ref}>
              {refOptionLabel(ref)}
            </option>
          ))}
        </Select>
      ) : (
        <ul className="connection-list">
          {value.map((code, index) => {
            const options = refs.filter((ref) => ref.ref === code || !used.has(ref.ref));
            return (
              <li className="connection-row" key={index}>
                <Select
                  id={`${idPrefix}-ref-${index}`}
                  aria-label={index === 0 ? "Primary reference" : `Ganged reference ${index + 1}`}
                  value={code}
                  onChange={(event) => setAt(index, event.currentTarget.value)}
                >
                  {options.map((ref) => (
                    <option key={ref.ref} value={ref.ref}>
                      {refOptionLabel(ref)}
                    </option>
                  ))}
                </Select>
                <span className="technical">{index === 0 ? "authoritative" : `#${index + 1}`}</span>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  aria-label={`Remove ${refLabelFor(refs, code)}`}
                  onClick={() => removeAt(index)}
                >
                  <X aria-hidden="true" className="size-4" />
                </Button>
              </li>
            );
          })}
        </ul>
      )}
      {value.length > 0 ? (
        <Button type="button" variant="secondary" onClick={addAnother} disabled={!canAddAnother}>
          + Gang another
        </Button>
      ) : null}
      {value.length > 1 ? (
        <p className="field-help">
          All references are set to the same value. The channel&apos;s hirer ceiling applies to the group as a whole (§5.5).
        </p>
      ) : null}
    </fieldset>
  );
}
