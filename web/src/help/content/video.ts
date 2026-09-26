/* HDMI matrix (spec §21.22, §7.5, §15.10). */
import type { HelpEntry } from "../types";

export const video = {
  "hdmi.refentity.ref": {
    term: "Physical input/output",
    body: "Which physical connector on the matrix this is. Only connectors the driver reports and nothing else already claims are offered.",
  },
  "hdmi.refentity.name": {
    term: "Name",
    body: "What this is called. Naming an input adds it to the operator's source buttons.",
  },
  "hdmi.refentity.description": {
    term: "Description",
    body: "What this is, for whoever configures it next.",
  },
  "hdmi.refentity.order": {
    term: "Order",
    body: "Where this sits relative to the others in its list.",
  },
  "hdmi.refentity.save": {
    term: "Save",
    body: "Saves this input or output.",
  },
  "hdmi.refentity.delete": {
    term: "Delete",
    body: "Removes it from the operator's source buttons. Refused if a scene still targets it.",
  },
  "hdmi.destinations.add": {
    term: "Add",
    body: "A destination is a named thing the venue routes video to, mapping to one or more physical outputs.",
  },
  "hdmi.destination.name": {
    term: "Name",
    body: "What this destination is called — what the operator picks.",
  },
  "hdmi.destination.outputs": {
    term: "Outputs",
    body: "Which physical outputs this destination switches, all in one driver call. Order matters when there is more than one — the first is authoritative for what the destination shows as switched to.",
  },
  "hdmi.destination.default-input": {
    term: "Default input",
    body: "What Restore Venue Default sets this destination to.",
  },
  "hdmi.destination.order": {
    term: "Order",
    body: "Where this destination sits relative to the others.",
  },
  "hdmi.destination.save": {
    term: "Save",
    body: "Saves this destination.",
  },
  "hdmi.destination.delete": {
    term: "Delete",
    body: "Refused while a scene still targets it. This does not delete its outputs.",
  },
} as const satisfies Record<string, HelpEntry>;
