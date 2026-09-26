/* Pages (spec §21.9, §15.12). The everyday operator surface. */
import type { HelpEntry } from "../types";

export const pages = {
  "pages.add": {
    term: "Add page",
    body: "Names a page and opens its editor. The generated default page cannot be edited or renamed, and always exists alongside whatever you add.",
  },
  "pages.new.name": {
    term: "Name",
    body: "What this page is called in the page list and, if assigned, in the hirer surface.",
  },
  "pages.save": {
    term: "Save page",
    body: "Saves this page's name and items.",
  },
  "pages.delete": {
    term: "Delete",
    body: "Removes the page and, if it was assigned, the hirer's access to it. This cannot be undone.",
  },
  "pages.name": {
    term: "Name",
    body: "What this page is called in the page list and, if assigned, in the hirer surface.",
  },
  "pages.item.channel": {
    term: "Channel",
    body: "Which mixer or lighting channel this strip controls.",
  },
  "pages.item.group": {
    term: "Group",
    body: "Which lighting group this master fader controls. Its member tray fills itself — edit membership on Lighting → Groups, not here.",
  },
  "pages.item.panel-title": {
    term: "Title",
    body: "The heading shown above this button panel.",
  },
  "pages.item.panel-width": {
    term: "Width",
    body: "How many buttons wide this panel is. Rows are unbounded — the layout works out how many fit per device.",
  },
  "pages.item.remove": {
    term: "Remove",
    body: "Takes this item off the page. Nothing is deleted anywhere else — a mixer channel, lighting channel or group keeps working exactly as before, it just stops appearing here.",
  },
  "pages.button.label": {
    term: "Label",
    body: "The text shown on this button.",
  },
  "pages.button.col": {
    term: "Column",
    body: "Which column of the panel this button sits in.",
  },
  "pages.button.row": {
    term: "Row",
    body: "Which row of the panel this button sits in. Rows are unbounded — the layout decides how many fit per device.",
  },
  "pages.button.rule": {
    term: "Fires rule",
    body: "Which rule this button runs when pressed, whatever that rule's own trigger type is.",
  },
  "pages.button.lamp": {
    term: "Lamp from",
    body: "A derived status to show as this button's indicator lamp, or none.",
  },
  "pages.button.colour": {
    term: "Colour",
    body: "This button's accent colour. Identity only — it never indicates status.",
  },
  "pages.button.save": {
    term: "Save button",
    body: "Saves this button.",
  },
  "pages.button.remove": {
    term: "Remove",
    body: "Takes this button off the panel. The rule it fired is not deleted.",
  },
} as const satisfies Record<string, HelpEntry>;
