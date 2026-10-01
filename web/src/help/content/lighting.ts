/* Lighting configuration (spec §21.18, §15.9, §9). */
import type { HelpEntry } from "../types";

export const lighting = {
  // Fixtures tab
  "lighting.fixtures.bar-filter": {
    term: "Bar",
    body: "Shows only the fixtures patched to this bar. Does not change anything — a filter, not a setting.",
  },
  "lighting.fixtures.type-filter": {
    term: "Type",
    body: "Shows only DMX or only KNX dimmer fixtures. Does not change anything.",
  },
  "lighting.fixtures.add": {
    term: "Add fixture",
    body: "Opens a new, unplaced fixture in the same sheet used to edit one.",
  },

  // Bars tab
  "lighting.bars.add": {
    term: "Add bar",
    body: "A bar is a lighting position — a pipe, a truss, a wall — that fixtures are patched to.",
  },
  "lighting.bar.name": {
    term: "Name",
    body: "What this bar is called on the stage plan and in the fixtures table.",
  },
  "lighting.bar.order": {
    term: "Order",
    body: "0 is downstage — nearest the proscenium — ascending upstage. Sets where the bar sits relative to the others.",
  },
  "lighting.bar.notes": {
    term: "Notes",
    body: "Anything worth remembering about this bar — rigging height, access, a house restriction. Shown alongside it, nowhere else.",
  },
  "lighting.bar.save": {
    term: "Save",
    body: "Saves this bar's name, order and notes.",
  },
  "lighting.bars.delete": {
    term: "Delete",
    body: "Deletes this bar immediately with no confirmation, unless it still has fixtures on it — then it asks where they should go first.",
  },
  "lighting.bars.move-and-delete": {
    term: "Move and delete bar",
    body: "Moves every fixture on this bar to the one chosen above, then deletes the now-empty bar.",
  },

  // Profiles tab
  "lighting.profiles.add": {
    term: "Add profile",
    body: "A profile describes what each of a fixture's DMX channels does — dimmer, colour, pan/tilt. Once added, it can be assigned to any DMX fixture.",
  },
  "lighting.profile.name": {
    term: "Name",
    body: "What this profile is called when choosing a fixture's type.",
  },
  "lighting.profile.manufacturer": {
    term: "Manufacturer",
    body: "Recorded for reference only — nothing reads it.",
  },
  "lighting.profile.model": {
    term: "Model",
    body: "Recorded for reference only — nothing reads it.",
  },
  "lighting.profile.channels": {
    term: "Channels",
    body: "The ordered list of DMX offsets this fixture occupies, starting from its patched address. The channel count follows this list's length — there is nothing separate to keep in step.",
  },
  "lighting.profile.channel-role": {
    term: "Role",
    body: "What this DMX offset controls. Only roles the system understands are offered — a custom role cannot be entered.",
  },
  "lighting.profile.channel-default": {
    term: "Default (DMX 0–255)",
    body: "The raw value sent on this channel when nothing else is driving it.",
  },
  "lighting.profile.save": {
    term: "Save",
    body: "Saves the profile. If fixtures already use it, their occupancy changes immediately; a conflict this creates is checked and shown after saving, not blocked here.",
  },
  "lighting.profiles.delete": {
    term: "Delete",
    body: "Deletes this profile immediately with no confirmation. Refused while any fixture still uses it.",
  },

  // Presets tab
  "lighting.presets.add": {
    term: "Add preset",
    body: "A preset is a one-tap colour for the operator's colour picker.",
  },
  "lighting.preset.name": {
    term: "Name",
    body: "What this preset is called in the operator's colour picker.",
  },
  "lighting.preset.channel": {
    term: "Colour component",
    body: "Red, green, blue or white, 0–255. Together they make the swatch shown here and in the operator's picker.",
  },
  "lighting.preset.save": {
    term: "Save",
    body: "Saves this preset.",
  },
  "lighting.presets.delete": {
    term: "Delete",
    body: "Deletes this preset. It stops appearing in the operator's colour picker; any lighting already set from it is not changed.",
  },

  // Groups tab
  "lighting.groups.add": {
    term: "Add group",
    body: "A group is a set of fixtures with one shared fader. Starts empty, named \"New group\" — open it to rename it and choose members.",
  },
  "lighting.group.name": {
    term: "Group name",
    body: "What this group is called on its fader and in group pickers.",
  },
  "lighting.group.colour": {
    term: "Colour",
    body: "The swatch shown against this group everywhere it appears. Identity only — it never indicates status (spec §21.3).",
  },
  "lighting.group.members": {
    term: "Members",
    body: "Which fixtures this group's fader controls. A fixture can belong to several groups; ticking one here never removes it from another.",
  },
  "lighting.group.indicator-only": {
    term: "Indicator only",
    body:
      "An indicator-only group has no fader anywhere and never dims its fixtures, so it can never hold another group's fixtures up or down. " +
      "It exists so a derived status can light a wall-panel lamp from its members — \"Stage all\" lighting the panel's all-on indicator, say. " +
      "The Master fader is the whole-stage fader. A binding cannot drive an indicator-only group; delete or re-point any binding on it first.",
  },
  "lighting.group.save": {
    term: "Save",
    body: "Saves this group's name, colour, whether it is indicator only, and its membership.",
  },
  "lighting.group.delete": {
    term: "Delete group",
    body: "Deletes this group immediately with no further confirmation. Its fixtures are not deleted — they just lose this group's fader.",
  },
  "lighting.group.create-name": {
    term: "Group name",
    body: "What the new group is called. Its members are exactly the fixtures selected on the stage plan.",
  },
  "lighting.group.create": {
    term: "Create group",
    body: "Creates the group from the current stage plan selection.",
  },

  // Fixture sheet (shared by Fixtures tab, Bars tab and the Stage Plan tab)
  "lighting.fixture.name": {
    term: "Name",
    body: "What this fixture is called on the stage plan, in the fixtures table and in group and page pickers.",
  },
  "lighting.fixture.type": {
    term: "Type",
    body: "The fixture profile to patch as DMX, or KNX dimmer for one addressed over KNX instead. Changing type replaces the fields below.",
  },
  "lighting.fixture.knx-command": {
    term: "Command address",
    body: "The KNX group address this fixture's level is written to. Required — without it there is nothing to control.",
  },
  "lighting.fixture.knx-status": {
    term: "Status address (optional)",
    body: "The KNX group address this fixture reports its actual level on, if it has one. Feeds the observed-state display only — never the control path.",
  },
  "lighting.fixture.knx-switch": {
    term: "Switch address (optional)",
    body: "A separate on/off group address, if this fixture needs one alongside its dimming address.",
  },
  "lighting.fixture.fade-mode": {
    term: "Fade mode",
    body: "Hardware lets the KNX actuator run its own ramp; software drives intermediate steps from here instead. Use software only where hardware fading is not available.",
  },
  "lighting.fixture.device": {
    term: "Output device",
    body: "Which configured DMX output this fixture is patched through.",
  },
  "lighting.fixture.universe": {
    term: "Universe",
    body: "The DMX universe on the chosen output device this fixture is patched into.",
  },
  "lighting.fixture.address": {
    term: "Start channel",
    body: "The first DMX channel this fixture occupies. Later channels follow automatically from the profile's channel count.",
  },
  "lighting.fixture.bar": {
    term: "Bar",
    body: "The lighting position this fixture is rigged on.",
  },
  "lighting.fixture.position": {
    term: "Position",
    body: "Where this fixture is drawn on the stage plan, stage right to stage left. Visual only — it has no effect on DMX.",
  },
  "lighting.fixture.min": {
    term: "Min level (%)",
    body: "The lowest level a fader or scene can send this fixture to.",
  },
  "lighting.fixture.max": {
    term: "Max level (%)",
    body: "The highest level a fader or scene can send this fixture to.",
  },
  "lighting.fixture.colour": {
    term: "Default colour",
    body: "The colour this fixture starts at. Drag the swatch or type R, G, B and W directly — both edit the same value.",
  },
  "lighting.fixture.visible": {
    term: "Visible to operator",
    body: "Turn off to keep this fixture out of the operator's lighting view — it still patches and runs in scenes, it just is not shown for individual control.",
  },
  "lighting.fixture.notes": {
    term: "Notes",
    body: "Anything worth remembering about this fixture. Shown alongside it, nowhere else.",
  },
  "lighting.fixture.save": {
    term: "Save fixture",
    body: "Saves this fixture. A patch conflict shown above is a warning, not a block — patching ahead of a rewire is normal during commissioning.",
  },
  "lighting.fixture.delete": {
    term: "Delete",
    body: "Removes the fixture from the patch and the stage plan. Refused while a saved look (a scene snapshot) still references it — group membership alone never blocks it.",
  },

  // Stage Plan tab's channel map
  "lighting.channel-map.universe": {
    term: "Universe",
    body: "Which DMX universe's channel map to show. Only appears when more than one is patched.",
  },

  // Stage Plan tab's quick "+ Add bar"
  "lighting.stageplan.add-bar-name": {
    term: "Bar name",
    body: "What the new bar is called. It appears upstage of every existing one — reorder it from the toolbar.",
  },
  "lighting.stageplan.add-bar": {
    term: "Add bar",
    body: "A quick way to add a bar without leaving the stage plan. The Bars tab covers full bar CRUD.",
  },
  "lighting.stageplan.move-conflict-overwrite": {
    term: "Overwrite with mine",
    body: "Saves the position you dragged to, replacing what the other change made.",
  },
  "lighting.bars.move-fixtures-to": {
    term: "Move fixtures to",
    body: "Where this bar's fixtures go before the bar itself is deleted. Unassigned is always a valid choice, even with no other bar.",
  },
  "lighting.stageplan.edit-mode": {
    term: "Edit mode",
    body: "On lets you drag fixtures and bars to reposition them. Off, the plan is live monitoring only, the same view an operator sees.",
  },
} as const satisfies Record<string, HelpEntry>;
