/* Mixer configuration (spec §21.21, §7.3, §5.5). */
import type { HelpEntry } from "../types";

export const mixer = {
  "mixer.channels.add": {
    term: "Add channel",
    body: "Adds a surface channel mapped to one of the desk's physical inputs.",
  },
  "mixer.channels.add-missing": {
    term: "Add missing channels",
    body: "Adds a channel for every channel on the desk that has none here, named from the desk and visible to staff. Channels you already have are left exactly as they are. Rename the new ones, or untick Staff on any operators shouldn't see; what a hirer sees is set by their pages.",
  },
  "mixer.outputs.add": {
    term: "Add output",
    body: "Adds a surface channel mapped to one of the desk's mix outputs.",
  },
  "mixer.channel.name": {
    term: "Name",
    body: "What this channel is called on the virtual surface. Does not have to match the desk's own numbering.",
  },
  "mixer.channel.short-name": {
    term: "Scribble strip override",
    body: "Only needed when the name, truncated to 7 characters, collides with another channel's. Leave blank to use the truncated name.",
  },
  "mixer.channel.refs": {
    term: "Maps to",
    body: "Which physical input or output on the desk this channel controls. Gang a second reference for a linked stereo pair — the desk does not report its own link state over MIDI, so this is how the admin says which pairing is in use.",
  },
  "mixer.channel.hirer-max": {
    term: "Hirer maximum",
    body: "The highest level a hirer can raise this channel to. No ceiling leaves it unrestricted.",
  },
  "mixer.channel.notes": {
    term: "Notes",
    body: "Anything worth recording about this channel. Shown alongside it, nowhere else.",
  },
  "mixer.channel.order": {
    term: "Order",
    body: "Where this channel sits relative to the others on the virtual surface.",
  },
  "mixer.channel.save": {
    term: "Save",
    body: "Saves this channel.",
  },
  "mixer.channel.delete": {
    term: "Delete",
    body: "Removes this channel from the virtual surface. Refused while a scene action still targets it.",
  },
  "mixer.output.delete": {
    term: "Delete",
    body: "Removes this output from the virtual surface. Refused while a scene action still targets it.",
  },
  "mixer.main.name": {
    term: "Name",
    body: "What the Main LR output is called on the virtual surface.",
  },
  "mixer.main.save": {
    term: "Save",
    body: "Saves the Main output's settings.",
  },
  "mixer.main.delete": {
    term: "Delete",
    body: "Main cannot actually be removed — this shows what the controller says when you try (§21.21).",
  },
  "mixer.deskscenes.add": {
    term: "Add scene",
    body: "Adds a library entry for one of the desk's own numbered scenes.",
  },
  "mixer.deskscene.number": {
    term: "CQ scene number",
    body: "The desk's own 1-based scene number. Scene actions reference this library entry, not the number, so renumbering in MixPad only needs this one row updated.",
  },
  "mixer.deskscene.name": {
    term: "Name",
    body: "What this desk scene is called wherever it can be chosen.",
  },
  "mixer.deskscene.description": {
    term: "Description",
    body: "What this desk scene sets up, for whoever picks it later.",
  },
  "mixer.deskscene.notes": {
    term: "Notes",
    body: "What the audio engineer knows about what this scene actually does — recorded nowhere else.",
  },
  "mixer.deskscene.test-recall": {
    term: "Test recall",
    body: "Recalls this scene on the desk immediately, so you can hear what it does. Unavailable where the configured driver does not support scene recall.",
  },
  "mixer.deskscene.save": {
    term: "Save",
    body: "Saves this desk scene entry. Marking it Venue Default clears that flag on every other one — exactly one can exist.",
  },
  "mixer.deskscene.delete": {
    term: "Delete",
    body: "Removes this library entry. Refused while a scene action still targets it.",
  },
} as const satisfies Record<string, HelpEntry>;
