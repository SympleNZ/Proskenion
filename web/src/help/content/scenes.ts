/* Scenes (spec §21.16, §8.11-§8.13). A scene is what happens; a rule decides when. */
import type { HelpEntry } from "../types";

export const scenes = {
  "scenes.new": {
    term: "New scene",
    body: "Names a scene and opens its editor — everything else can be set there.",
  },
  "scenes.new.name": {
    term: "Name",
    body: "What this scene is called wherever it can be chosen — pages, rules, the execution log.",
  },
  "scenes.new.create": {
    term: "Create",
    body: "Creates the scene and opens its editor.",
  },
  "scenes.save": {
    term: "Save",
    body: "Saves this scene's details. Its actions save individually, as each is added or edited.",
  },
  "scenes.name": {
    term: "Name",
    body: "What this scene is called wherever it can be chosen — pages, rules, the execution log.",
  },
  "scenes.description": {
    term: "Description",
    body: "What this scene does, for whoever picks it later.",
  },
  "scenes.icon": {
    term: "Icon",
    body: "One emoji shown next to this scene's name.",
  },
  "scenes.priority": {
    term: "Priority",
    body: "Critical scenes get the extra test gate before running live (§8.11) — use it for anything the show depends on.",
  },
  "scenes.action.delay": {
    term: "Delay (ms)",
    body: "When this action fires, relative to the scene starting. Actions sharing a delay fire together as one group.",
  },
  "scenes.action.dmx.capture": {
    term: "Capture current look",
    body: "Snapshots every DMX channel's level right now and stores it as this action's look. Re-capture any time before saving to replace it.",
  },
  "scenes.action.dmx.fade": {
    term: "Fade (ms)",
    body: "How long the captured look takes to reach its levels. A non-zero fade shows as \"DMX fade\" in the timeline.",
  },
  "scenes.action.knx.address": {
    term: "Address",
    body: "The KNX group address this action writes to. Only outgoing addresses are offered.",
  },
  "scenes.action.knx.source": {
    term: "Value",
    body: "A literal value typed here, or the value of whatever triggered the rule that ran this scene, optionally scaled.",
  },
  "scenes.action.knx.literal": {
    term: "Literal value",
    body: "The value written to the address, in the format its data point type expects.",
  },
  "scenes.action.knx.scale": {
    term: "Scale (optional)",
    body: "Multiplies the trigger's numeric value before writing it. Numeric addresses only.",
  },
  "scenes.action.projector.power": {
    term: "Power",
    body: "Turns the projector on or off.",
  },
  "scenes.action.projector.input": {
    term: "Input",
    body: "Which projector input to switch to. Rejected, not queued, while the projector is warming, cooling or off.",
  },
  "scenes.action.hdmi.destination": {
    term: "Destination",
    body: "Which HDMI matrix output this action switches.",
  },
  "scenes.action.hdmi.source": {
    term: "Source",
    body: "Which input to route to the destination, or Venue default to restore its normal routing.",
  },
  "scenes.action.mixer.recall": {
    term: "Desk scene",
    body: "Which mixer desk scene to recall, or Venue Default. Unavailable where the configured mixer's driver does not support scene recall.",
  },
  "scenes.action.mixer.channel": {
    term: "Channel",
    body: "Which mixer channel this action controls.",
  },
  "scenes.action.mixer.level": {
    term: "Level",
    body: "The fader level this action sets the channel to, or Off.",
  },
  "scenes.action.mixer.mute": {
    term: "Mute",
    body: "Always an absolute set, never a toggle — this action always leaves the channel in the same state.",
  },
  "scenes.action.save": {
    term: "Save action",
    body: "Saves this action.",
  },
  "scenes.guard.save-and-leave": {
    term: "Save and leave",
    body: "Saves the scene's details, then returns to the scene list.",
  },
  "scenes.delete": {
    term: "Delete",
    body: "Removes the scene and every one of its actions. A scene referenced by a rule cannot be deleted (§15.8).",
  },
} as const satisfies Record<string, HelpEntry>;
