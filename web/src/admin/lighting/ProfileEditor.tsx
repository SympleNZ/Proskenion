/*
 * The fixture profile editor (spec §21.18 *Fixture profiles*, §15.9): name,
 * manufacturer, model and the ordered channel list. Each channel has an
 * offset (its position in the list), a role from the closed vocabulary only,
 * and a DMX 0–255 default. `channel_count` follows the list length rather
 * than being entered separately — the list *is* the definition.
 */
import { useId, useState } from "react";

import { Banner } from "@/components/ui/Banner";
import { Button } from "@/components/ui/Button";
import { Select } from "@/components/ui/Select";
import { Input } from "@/components/ui/Input";
import { Sheet, SheetContent } from "@/components/ui/Sheet";
import { FieldLabel, HelpButton } from "@/help/HelpButton";
import { FIXTURE_PROFILE_ROLES, type FixtureProfile, type FixtureProfileRole } from "@/lighting/types";
import { saveOnShortcut } from "@/lib/keyboard";

import type { ProfileChannelInput } from "./types";

const ROLE_LABELS: Record<FixtureProfileRole, string> = {
  dimmer: "Dimmer",
  red: "Red",
  green: "Green",
  blue: "Blue",
  white: "White",
  amber: "Amber",
  uv: "UV",
  pan: "Pan",
  tilt: "Tilt",
  strobe: "Strobe",
  macro: "Macro",
  unused: "Unused",
};

export interface ProfileEditorInput {
  name: string;
  manufacturer: string | null;
  model: string | null;
  channels: readonly ProfileChannelInput[];
}

export interface ProfileEditorProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** `null` starts a blank profile; a "(copy)" duplicate is passed as a profile with no `id` reuse — the caller decides what to `POST` (§21.18's duplicate-and-edit). */
  profile: FixtureProfile | null;
  /** How many patched fixtures use this profile — editing it changes their occupancy (§21.18). */
  fixturesUsingCount: number;
  saving: boolean;
  onSave: (input: ProfileEditorInput) => void;
}

function emptyChannel(offset: number): ProfileChannelInput {
  return { offset, role: "unused", default: 0 };
}

export function ProfileEditor({ open, onOpenChange, profile, fixturesUsingCount, saving, onSave }: ProfileEditorProps) {
  const [name, setName] = useState(profile?.name ?? "");
  const [manufacturer, setManufacturer] = useState(profile?.manufacturer ?? "");
  const [model, setModel] = useState(profile?.model ?? "");
  const [channels, setChannels] = useState<ProfileChannelInput[]>(
    profile ? profile.channels.map((c) => ({ offset: c.offset, role: c.role, default: c.default })) : [emptyChannel(0)],
  );
  const idPrefix = useId();

  function setChannel(index: number, patch: Partial<ProfileChannelInput>): void {
    setChannels((current) => current.map((c, i) => (i === index ? { ...c, ...patch } : c)));
  }

  function addChannel(): void {
    setChannels((current) => [...current, emptyChannel(current.length)]);
  }

  function removeChannel(index: number): void {
    setChannels((current) => current.filter((_, i) => i !== index).map((c, i) => ({ ...c, offset: i })));
  }

  const canSave = name.trim().length > 0 && channels.length > 0;

  function save(): void {
    if (!canSave) return;
    onSave({
      name: name.trim(),
      manufacturer: manufacturer.trim().length > 0 ? manufacturer.trim() : null,
      model: model.trim().length > 0 ? model.trim() : null,
      channels,
    });
  }

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent title={profile ? `Edit ${profile.name}` : "Add fixture profile"} side="right" onKeyDown={saveOnShortcut(save)}>
        <div className="field">
          <FieldLabel htmlFor={`${idPrefix}-name`} help="lighting.profile.name">
            Name
          </FieldLabel>
          <Input id={`${idPrefix}-name`} value={name} onChange={(event) => setName(event.currentTarget.value)} autoFocus />
        </div>
        <div className="field">
          <FieldLabel htmlFor={`${idPrefix}-manufacturer`} help="lighting.profile.manufacturer">
            Manufacturer
          </FieldLabel>
          <Input id={`${idPrefix}-manufacturer`} value={manufacturer} onChange={(event) => setManufacturer(event.currentTarget.value)} />
        </div>
        <div className="field">
          <FieldLabel htmlFor={`${idPrefix}-model`} help="lighting.profile.model">
            Model
          </FieldLabel>
          <Input id={`${idPrefix}-model`} value={model} onChange={(event) => setModel(event.currentTarget.value)} />
        </div>

        {fixturesUsingCount > 0 ? (
          <Banner tone="warning" title={`${fixturesUsingCount} fixture${fixturesUsingCount === 1 ? "" : "s"} use this profile`}>
            Changing the channel list changes their occupancy. A new length that creates a patch conflict is not blocked here — it is
            checked against the patch after saving.
          </Banner>
        ) : null}

        <div className="field">
          <div className="field-label-row">
            <span className="field-label">Channels — {channels.length} total</span>
            <HelpButton id="lighting.profile.channels" />
          </div>
          <ul className="flex flex-col gap-2">
            {channels.map((channel, index) => (
              <li key={index} className="profile-channel-row">
                <span className="technical text-fg-muted">#{channel.offset}</span>
                <div className="field">
                  <FieldLabel htmlFor={`${idPrefix}-role-${index}`} help="lighting.profile.channel-role">
                    Role
                  </FieldLabel>
                  <Select
                    id={`${idPrefix}-role-${index}`}
                    value={channel.role}
                    onChange={(event) => setChannel(index, { role: event.currentTarget.value })}
                  >
                    {FIXTURE_PROFILE_ROLES.map((role) => (
                      <option key={role} value={role}>
                        {ROLE_LABELS[role]}
                      </option>
                    ))}
                  </Select>
                </div>
                <div className="field">
                  <FieldLabel htmlFor={`${idPrefix}-default-${index}`} help="lighting.profile.channel-default">
                    Default (DMX 0–255)
                  </FieldLabel>
                  <Input
                    id={`${idPrefix}-default-${index}`}
                    type="number"
                    mono
                    min={0}
                    max={255}
                    value={channel.default}
                    onChange={(event) => setChannel(index, { default: Number(event.currentTarget.value) })}
                  />
                </div>
                <Button
                  variant="ghost"
                  size="icon"
                  aria-label={`Remove channel ${channel.offset}`}
                  disabled={channels.length <= 1}
                  onClick={() => removeChannel(index)}
                >
                  ✕
                </Button>
              </li>
            ))}
          </ul>
          <Button variant="secondary" onClick={addChannel}>
            + Add channel
          </Button>
        </div>

        <div className="dialog-actions">
          <Button variant="secondary" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            variant="primary"
            helpId="lighting.profile.save"
            loading={saving}
            disabled={!canSave}
            onClick={save}
          >
            Save
          </Button>
        </div>
      </SheetContent>
    </Sheet>
  );
}
