/*
 * The fixture sheet (spec §21.18): one sheet for add and edit, grown from
 * what used to be `AddFixtureSheet` — a minimal placement form good enough
 * for the stage plan's own "Add fixture" button. §21.18 predates fixture
 * profiles (§15.9): its "Type: Dimmer | RGB | RGBW" is the choice of fixture
 * profile for a DMX fixture, or KNX dimmer, so the Type field here lists
 * every profile plus one "KNX dimmer" entry rather than a fixed three-way enum.
 *
 * Occupancy recalculates live from the chosen profile and start address
 * (§21.18, §9.1); the patch-conflict banner does too, computed purely from
 * what is on screen (`../admin/lighting/patchConflict.ts`) so it updates
 * before anything is saved. A conflict warns; it never blocks saving —
 * patching ahead of a rewire is a legitimate commissioning state (§9.1).
 */
import { useId, useMemo, useState } from "react";

import type { Device } from "@/admin/devices/types";
import type { KnxAddress } from "@/admin/lighting/types";
import { localPatchConflicts } from "@/admin/lighting/patchConflict";
import { hsvToRgb, rgbToHsv } from "@/admin/lighting/hsv";
import { describeOccupancy } from "@/admin/lighting/occupancy";
import { Banner } from "@/components/ui/Banner";
import { Button } from "@/components/ui/Button";
import { Checkbox, Select } from "@/components/ui/Select";
import { Input } from "@/components/ui/Input";
import { Sheet, SheetContent } from "@/components/ui/Sheet";
import { FieldLabel, HelpButton } from "@/help/HelpButton";
import { colourToCss } from "@/lighting/colour";
import { COLOUR_ROLES, type FadeMode, type FixtureProfile, type LightingChannel, type LightingGroup, type LightingReference } from "@/lighting/types";

import { FixtureDeleteDialog } from "./FixtureDeleteDialog";
import type { LightingBar } from "./types";

/** The body this sheet builds on Save — matches `FixtureInput` in `@/admin/lighting/types` without importing admin/ as a value dependency. */
export interface FixtureSheetInput {
  name: string;
  type: "dmx" | "knx_dimmer";
  bar_id: number | null;
  position: number | null;
  min_value: number;
  max_value: number;
  visible_staff: boolean;
  notes: string | null;
  profile_id?: number | null;
  device_id?: number | null;
  universe?: number;
  address?: number | null;
  colour_r?: number | null;
  colour_g?: number | null;
  colour_b?: number | null;
  colour_w?: number | null;
  knx_command_address_id?: number | null;
  knx_status_address_id?: number | null;
  knx_switch_address_id?: number | null;
  fade_mode?: FadeMode;
}

export interface FixtureSheetProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** `null` adds a new fixture; otherwise the fixture being edited. */
  fixture: LightingChannel | null;
  bars: readonly LightingBar[];
  /** Every fixture profile — the Type field's DMX options (§15.9). */
  profiles: readonly FixtureProfile[];
  /** Lighting-category output devices (§5.5) — already filtered by the caller. */
  devices: readonly Device[];
  knxAddresses: readonly KnxAddress[];
  /** Every other fixture, for the live patch-conflict warning (§9.1). */
  channels: readonly LightingChannel[];
  /** Pre-fills the bar in add mode — "Add" on a bar row opens the sheet pre-filled with it (§21.18). */
  initialBarId?: number | null;
  saving: boolean;
  onSave: (input: FixtureSheetInput) => void;
  /** Per-field messages from a `422 validation_failed` save (§16.1) — a dmx row missing its profile/device/address, or a knx_dimmer missing its command address. */
  fieldErrors?: Readonly<Record<string, string>>;

  // Delete (§21.18, §9.7) — only offered in edit mode.
  deleting?: boolean;
  onDelete?: (() => void) | undefined;
  groups?: readonly LightingGroup[];
  references?: readonly LightingReference[];
  referencesLoading?: boolean;
}

/**
 * Only the two values `proskenion/core/dmx/compositor.py`'s `FadeMode`
 * actually honours (see the `FadeMode` doc comment in `@/lighting/types`) —
 * a third `hardware_timed` the schema comment names is silently driven as
 * `hardware`, so it is not offered here.
 */
const FADE_MODE_LABELS: Record<FadeMode, string> = {
  hardware: "Hardware",
  software: "Software",
};

type TypeValue = `profile:${number}` | "knx";

function typeValueOf(fixture: LightingChannel | null, profiles: readonly FixtureProfile[]): TypeValue {
  if (fixture?.type === "knx_dimmer") return "knx";
  const profileId = fixture?.profile_id ?? profiles[0]?.id;
  if (profileId !== undefined && profileId !== null) return `profile:${profileId}`;
  // No profile to default to yet (profiles have not loaded) — the DMX
  // placeholder option, never a silent jump to KNX dimmer.
  return "profile:0";
}

export function FixtureSheet({
  open,
  onOpenChange,
  fixture,
  bars,
  profiles,
  devices,
  knxAddresses,
  channels,
  initialBarId,
  saving,
  onSave,
  fieldErrors = {},
  deleting = false,
  onDelete,
  groups = [],
  references = [],
  referencesLoading = false,
}: FixtureSheetProps) {
  const editing = fixture !== null;
  const idPrefix = useId();

  const [name, setName] = useState(fixture?.name ?? "");
  const [typeValue, setTypeValue] = useState<TypeValue>(() => typeValueOf(fixture, profiles));
  const [barId, setBarId] = useState<number | "">(fixture?.bar_id ?? initialBarId ?? bars[0]?.id ?? "");
  const [position, setPosition] = useState(fixture?.position ?? 0.5);
  const [minValue, setMinValue] = useState(fixture?.min_value ?? 0);
  const [maxValue, setMaxValue] = useState(fixture?.max_value ?? 100);
  const [visibleStaff, setVisibleStaff] = useState(fixture?.visible_staff ?? true);
  const [notes, setNotes] = useState(fixture?.notes ?? "");

  const [deviceId, setDeviceId] = useState<number | "">(fixture?.device_id ?? devices[0]?.id ?? "");
  const [universe, setUniverse] = useState(fixture?.universe ?? 1);
  const [address, setAddress] = useState(fixture?.address ?? 1);

  const [commandAddressId, setCommandAddressId] = useState<number | "">(fixture?.knx_command_address_id ?? "");
  const [statusAddressId, setStatusAddressId] = useState<number | "">(fixture?.knx_status_address_id ?? "");
  const [switchAddressId, setSwitchAddressId] = useState<number | "">(fixture?.knx_switch_address_id ?? "");
  const [fadeMode, setFadeMode] = useState<FadeMode>(fixture?.fade_mode ?? "hardware");

  const [colourR, setColourR] = useState(fixture?.colour_r ?? 0);
  const [colourG, setColourG] = useState(fixture?.colour_g ?? 0);
  const [colourB, setColourB] = useState(fixture?.colour_b ?? 0);
  const [colourW, setColourW] = useState(fixture?.colour_w ?? 0);

  const [deleteOpen, setDeleteOpen] = useState(false);

  const isKnx = typeValue === "knx";
  const selectedProfile = useMemo(() => {
    if (isKnx) return undefined;
    const id = Number(typeValue.slice("profile:".length));
    return profiles.find((p) => p.id === id);
  }, [isKnx, typeValue, profiles]);

  const channelCount = selectedProfile?.channel_count ?? 1;
  const occupies = describeOccupancy(isKnx ? null : address, channelCount);
  const hasColour = !isKnx && (selectedProfile?.channels ?? []).some((c) => COLOUR_ROLES.has(c.role));
  const hasWhite = !isKnx && (selectedProfile?.channels ?? []).some((c) => c.role === "white");

  const conflictNames = useMemo(() => {
    if (isKnx || deviceId === "") return [];
    return localPatchConflicts(channels, profiles, {
      channelId: fixture?.id ?? null,
      deviceId,
      universe,
      address,
      channelCount,
    });
  }, [isKnx, deviceId, universe, address, channelCount, channels, profiles, fixture?.id]);

  const hsv = rgbToHsv({ r: colourR, g: colourG, b: colourB });

  function applyHsv(next: { h: number; s: number; v: number }): void {
    const rgb = hsvToRgb(next);
    setColourR(rgb.r);
    setColourG(rgb.g);
    setColourB(rgb.b);
  }

  const nameValid = name.trim().length > 0;
  const barValid = barId !== "";
  const knxValid = !isKnx || commandAddressId !== "";
  const dmxValid = isKnx || (deviceId !== "" && selectedProfile !== undefined);
  const canSave = nameValid && barValid && knxValid && dmxValid;

  function submit(): void {
    if (!canSave) return;
    const base = {
      name: name.trim(),
      // `canSave` (checked above) already guarantees `barValid`, so `barId`
      // is never "" here — TypeScript's own control-flow narrowing agrees.
      bar_id: barId,
      position,
      min_value: minValue,
      max_value: maxValue,
      visible_staff: visibleStaff,
      notes: notes.trim().length > 0 ? notes.trim() : null,
    };
    if (isKnx) {
      onSave({
        ...base,
        type: "knx_dimmer",
        knx_command_address_id: commandAddressId === "" ? null : commandAddressId,
        knx_status_address_id: statusAddressId === "" ? null : statusAddressId,
        knx_switch_address_id: switchAddressId === "" ? null : switchAddressId,
        fade_mode: fadeMode,
        // A row is one shape or the other, never half of each (§15.9), and the
        // server checks the row as it will be after the update: a fixture
        // changing from DMX must clear its DMX fields.
        profile_id: null,
        device_id: null,
        address: null,
        colour_r: null,
        colour_g: null,
        colour_b: null,
        colour_w: null,
      });
    } else {
      onSave({
        ...base,
        type: "dmx",
        profile_id: selectedProfile?.id ?? null,
        device_id: deviceId === "" ? null : deviceId,
        universe,
        address,
        colour_r: hasColour ? colourR : null,
        colour_g: hasColour ? colourG : null,
        colour_b: hasColour ? colourB : null,
        colour_w: hasColour && hasWhite ? colourW : null,
        // As above: a fixture changing from a KNX dimmer clears its addresses.
        knx_command_address_id: null,
        knx_status_address_id: null,
        knx_switch_address_id: null,
      });
    }
  }

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent
        title={editing ? `Edit fixture — ${fixture.name}` : "Add fixture"}
        description={editing ? "Changes apply once saved." : "Places a new fixture on the stage plan and in the patch."}
      >
        <div className="field">
          <FieldLabel htmlFor={`${idPrefix}-name`} help="lighting.fixture.name">
            Name
          </FieldLabel>
          <Input id={`${idPrefix}-name`} value={name} onChange={(event) => setName(event.currentTarget.value)} autoFocus />
        </div>

        <div className="field">
          <FieldLabel htmlFor={`${idPrefix}-type`} help="lighting.fixture.type">
            Type
          </FieldLabel>
          <Select id={`${idPrefix}-type`} value={typeValue} onChange={(event) => setTypeValue(event.currentTarget.value as TypeValue)}>
            {profiles.length === 0 ? <option value="profile:0">No profiles yet</option> : null}
            {profiles.map((profile) => (
              <option key={profile.id} value={`profile:${profile.id}`}>
                {profile.name}
              </option>
            ))}
            <option value="knx">KNX dimmer</option>
          </Select>
        </div>

        {isKnx ? (
          <>
            <div className="field">
              <FieldLabel htmlFor={`${idPrefix}-command`} help="lighting.fixture.knx-command">
                Command address
              </FieldLabel>
              <Select
                id={`${idPrefix}-command`}
                value={commandAddressId}
                onChange={(event) => setCommandAddressId(event.currentTarget.value === "" ? "" : Number(event.currentTarget.value))}
              >
                <option value="">Choose an address</option>
                {knxAddresses.map((address_) => (
                  <option key={address_.id} value={address_.id}>
                    {address_.name} — {address_.group_address}
                  </option>
                ))}
              </Select>
            </div>
            <div className="field">
              <FieldLabel htmlFor={`${idPrefix}-status`} help="lighting.fixture.knx-status">
                Status address (optional)
              </FieldLabel>
              <Select
                id={`${idPrefix}-status`}
                value={statusAddressId}
                onChange={(event) => setStatusAddressId(event.currentTarget.value === "" ? "" : Number(event.currentTarget.value))}
              >
                <option value="">None</option>
                {knxAddresses.map((address_) => (
                  <option key={address_.id} value={address_.id}>
                    {address_.name} — {address_.group_address}
                  </option>
                ))}
              </Select>
            </div>
            <div className="field">
              <FieldLabel htmlFor={`${idPrefix}-switch`} help="lighting.fixture.knx-switch">
                Switch address (optional)
              </FieldLabel>
              <Select
                id={`${idPrefix}-switch`}
                value={switchAddressId}
                onChange={(event) => setSwitchAddressId(event.currentTarget.value === "" ? "" : Number(event.currentTarget.value))}
              >
                <option value="">None</option>
                {knxAddresses.map((address_) => (
                  <option key={address_.id} value={address_.id}>
                    {address_.name} — {address_.group_address}
                  </option>
                ))}
              </Select>
            </div>
            <div className="field">
              <FieldLabel htmlFor={`${idPrefix}-fade-mode`} help="lighting.fixture.fade-mode">
                Fade mode
              </FieldLabel>
              <Select id={`${idPrefix}-fade-mode`} value={fadeMode} onChange={(event) => setFadeMode(event.currentTarget.value as FadeMode)}>
                {(Object.entries(FADE_MODE_LABELS) as [FadeMode, string][]).map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </Select>
            </div>
          </>
        ) : (
          <>
            <div className="field">
              <FieldLabel htmlFor={`${idPrefix}-device`} help="lighting.fixture.device">
                Output device
              </FieldLabel>
              <Select
                id={`${idPrefix}-device`}
                value={deviceId}
                onChange={(event) => setDeviceId(event.currentTarget.value === "" ? "" : Number(event.currentTarget.value))}
              >
                <option value="">Choose a device</option>
                {devices.map((device) => (
                  <option key={device.id} value={device.id}>
                    {device.name}
                  </option>
                ))}
              </Select>
            </div>
            <div className="field">
              <FieldLabel htmlFor={`${idPrefix}-universe`} help="lighting.fixture.universe">
                Universe
              </FieldLabel>
              <Input
                id={`${idPrefix}-universe`}
                type="number"
                mono
                min={1}
                value={universe}
                onChange={(event) => setUniverse(Number(event.currentTarget.value))}
              />
            </div>
            <div className="field">
              <FieldLabel htmlFor={`${idPrefix}-address`} help="lighting.fixture.address">
                Start channel
              </FieldLabel>
              <Input
                id={`${idPrefix}-address`}
                type="number"
                mono
                min={1}
                max={512}
                value={address}
                onChange={(event) => setAddress(Number(event.currentTarget.value))}
              />
            </div>
            <p className="field-help">Occupies — {occupies}</p>
          </>
        )}

        <div className="field">
          <FieldLabel htmlFor={`${idPrefix}-bar`} help="lighting.fixture.bar">
            Bar
          </FieldLabel>
          <Select
            id={`${idPrefix}-bar`}
            value={barId}
            onChange={(event) => setBarId(event.currentTarget.value === "" ? "" : Number(event.currentTarget.value))}
          >
            {bars.length === 0 ? <option value="">No bars yet</option> : null}
            {bars.map((bar) => (
              <option key={bar.id} value={bar.id}>
                {bar.name}
              </option>
            ))}
          </Select>
        </div>

        <div className="field">
          <FieldLabel htmlFor={`${idPrefix}-position`} help="lighting.fixture.position">
            Position (0.0 stage right – 1.0 stage left)
          </FieldLabel>
          <input
            id={`${idPrefix}-position`}
            type="range"
            min={0}
            max={1}
            step={0.01}
            value={position}
            onChange={(event) => setPosition(Number(event.target.value))}
          />
          <p className="field-help">Visual position only — does not affect DMX.</p>
        </div>

        <div className="field">
          <FieldLabel htmlFor={`${idPrefix}-min`} help="lighting.fixture.min">
            Min level (%)
          </FieldLabel>
          <Input
            id={`${idPrefix}-min`}
            type="number"
            mono
            min={0}
            max={100}
            step={0.1}
            value={minValue}
            onChange={(event) => setMinValue(Number(event.currentTarget.value))}
          />
        </div>
        <div className="field">
          <FieldLabel htmlFor={`${idPrefix}-max`} help="lighting.fixture.max">
            Max level (%)
          </FieldLabel>
          <Input
            id={`${idPrefix}-max`}
            type="number"
            mono
            min={0}
            max={100}
            step={0.1}
            value={maxValue}
            onChange={(event) => setMaxValue(Number(event.currentTarget.value))}
          />
        </div>

        {hasColour ? (
          <div className="field">
            <div className="field-label-row">
              <span className="field-label">Default colour</span>
              <HelpButton id="lighting.fixture.colour" />
            </div>
            <div className="flex items-center gap-3">
              <span aria-hidden="true" className="fixture-sheet-swatch" style={{ background: colourToCss({ r: colourR, g: colourG, b: colourB, w: colourW }) }} />
              <div className="flex flex-1 flex-col gap-2">
                <label className="field-help" htmlFor={`${idPrefix}-hue`}>
                  Hue
                </label>
                <input
                  id={`${idPrefix}-hue`}
                  type="range"
                  min={0}
                  max={360}
                  step={1}
                  value={hsv.h}
                  onChange={(event) => applyHsv({ ...hsv, h: Number(event.target.value) })}
                />
                <label className="field-help" htmlFor={`${idPrefix}-sat`}>
                  Saturation
                </label>
                <input
                  id={`${idPrefix}-sat`}
                  type="range"
                  min={0}
                  max={100}
                  step={1}
                  value={hsv.s}
                  onChange={(event) => applyHsv({ ...hsv, s: Number(event.target.value) })}
                />
                <label className="field-help" htmlFor={`${idPrefix}-val`}>
                  Value
                </label>
                <input
                  id={`${idPrefix}-val`}
                  type="range"
                  min={0}
                  max={100}
                  step={1}
                  value={hsv.v}
                  onChange={(event) => applyHsv({ ...hsv, v: Number(event.target.value) })}
                />
              </div>
            </div>
            <div className="grid grid-cols-4 gap-2">
              <div className="field">
                <FieldLabel htmlFor={`${idPrefix}-r`} help="lighting.fixture.colour">
                  R
                </FieldLabel>
                <Input id={`${idPrefix}-r`} type="number" mono min={0} max={255} value={colourR} onChange={(e) => setColourR(Number(e.currentTarget.value))} />
              </div>
              <div className="field">
                <FieldLabel htmlFor={`${idPrefix}-g`} help="lighting.fixture.colour">
                  G
                </FieldLabel>
                <Input id={`${idPrefix}-g`} type="number" mono min={0} max={255} value={colourG} onChange={(e) => setColourG(Number(e.currentTarget.value))} />
              </div>
              <div className="field">
                <FieldLabel htmlFor={`${idPrefix}-b`} help="lighting.fixture.colour">
                  B
                </FieldLabel>
                <Input id={`${idPrefix}-b`} type="number" mono min={0} max={255} value={colourB} onChange={(e) => setColourB(Number(e.currentTarget.value))} />
              </div>
              <div className="field">
                <FieldLabel htmlFor={`${idPrefix}-w`} help="lighting.fixture.colour">
                  W
                </FieldLabel>
                <Input
                  id={`${idPrefix}-w`}
                  type="number"
                  mono
                  min={0}
                  max={255}
                  disabled={!hasWhite}
                  value={hasWhite ? colourW : ""}
                  placeholder={hasWhite ? undefined : "—"}
                  onChange={(e) => setColourW(Number(e.currentTarget.value))}
                />
              </div>
            </div>
          </div>
        ) : null}

        <Checkbox
          id={`${idPrefix}-visible`}
          label="Visible to operator"
          checked={visibleStaff}
          onChange={(event) => setVisibleStaff(event.currentTarget.checked)}
        />

        <div className="field">
          <FieldLabel htmlFor={`${idPrefix}-notes`} help="lighting.fixture.notes">
            Notes
          </FieldLabel>
          <textarea
            id={`${idPrefix}-notes`}
            className="input"
            rows={3}
            value={notes}
            onChange={(event) => setNotes(event.currentTarget.value)}
          />
        </div>

        {Object.keys(fieldErrors).length > 0 ? (
          <Banner tone="danger" title="This could not be saved">
            {Object.entries(fieldErrors).map(([field, message]) => (
              <p key={field}>{message}</p>
            ))}
          </Banner>
        ) : null}

        {conflictNames.length > 0 ? (
          <Banner tone="warning" title="Channel conflict">
            Overlaps {conflictNames.map((n) => `"${n}"`).join(", ")}. Saving is still allowed — patching ahead of a rewire is normal during
            commissioning.
          </Banner>
        ) : null}

        <div className="dialog-actions">
          <Button variant="secondary" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          {editing && onDelete ? (
            <Button variant="destructive" helpId="lighting.fixture.delete" onClick={() => setDeleteOpen(true)}>
              Delete
            </Button>
          ) : null}
          <Button variant="primary" helpId="lighting.fixture.save" loading={saving} disabled={!canSave} onClick={submit}>
            {editing ? "Save" : "Add fixture"}
          </Button>
        </div>

        {editing && onDelete ? (
          <FixtureDeleteDialog
            open={deleteOpen}
            onOpenChange={setDeleteOpen}
            fixtureName={fixture.name}
            groupNames={groups.filter((g) => fixture.group_ids.includes(g.id)).map((g) => g.name)}
            references={references}
            loading={referencesLoading}
            deleting={deleting}
            onConfirm={() => {
              setDeleteOpen(false);
              onDelete();
            }}
          />
        ) : null}
      </SheetContent>
    </Sheet>
  );
}
