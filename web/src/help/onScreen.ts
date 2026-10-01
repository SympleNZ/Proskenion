/*
 * Per-screen content for the "On this screen" section of the `?` sheet
 * (spec §21.24 *Help*, Simon's 28 Sep request). Two things live here:
 *
 *  - a short, plain-language summary of what each screen in
 *    `web/src/navigation.ts` is for, written from the spec section and the
 *    screen's own top-of-file doc comment — never invented, and kept to a
 *    sentence or two for school staff reading it on a touch screen;
 *  - `collectOnScreenEntries`, which finds the inline help (ⓘ) actually
 *    rendered on the current screen by walking the DOM for
 *    `[data-help-trigger]` — the same marker `coverage.ts` already uses to
 *    find them for its own, different purpose — rather than a second,
 *    hand-maintained list of which help id belongs to which screen.
 *
 * `registry.ts` is not organised by screen: each control declares its own
 * `helpId` where it is used (`HelpButton`, `FieldLabel`), so the same text
 * can be reached from wherever the control actually lives, and a control
 * that moves between cards carries its help with it with no second edit. A
 * parallel "screen -> [ids]" map here would have to be kept in step with
 * that by hand, and would drift silently the moment someone added or moved
 * a control without touching it. Reading the rendered DOM instead can never
 * drift out of step with what is on screen: it finds exactly the ⓘ buttons
 * the person looking at the sheet can also see and click, already in the
 * order they appear (`querySelectorAll` visits the tree in document order,
 * which is reading order here — nothing in this codebase reorders a panel's
 * controls with CSS `order`).
 *
 * Screens are resolved from the URL, not the tier: `/admin/...` and
 * `/app/...` name two different screens for the same nav label (the admin
 * Mixer *configuration* screen versus the operator Mixer *control* view are
 * different components with different help entries), so the path alone
 * picks the right one. Tier is still checked in `resolveScreen`, not because
 * the path is ambiguous but because a hirer or operator should see nothing
 * here at all for a path routing itself never sends them to (`RequireTier`'s
 * own `allow` lists, in `routes/guards.tsx`) — belt and braces, since this
 * reads the URL directly rather than through a matched route.
 */
import type { Tier } from "@/api/auth";

import type { DocId } from "./docs/docs";
import { HELP_CONTENT, type HelpId } from "./registry";
import type { HelpEntry } from "./types";

export interface ScreenDocLink {
  id: DocId;
  /** The exact heading text in that document, matched against the rendered heading (`DocsSection.tsx`) — not a slug. */
  heading: string;
}

export interface ScreenHelp {
  /** A sentence or two, plain language, for school staff. */
  summary: string;
  doc?: ScreenDocLink;
}

const OPERATOR_QUICK_REFERENCE_SCREENS: ScreenDocLink = { id: "operator-quick-reference", heading: "The screens" };

/** Admin nav items (`web/src/navigation.ts`'s `ADMIN_NAV`/`ADMIN_ITEMS`), one summary each. */
export const ADMIN_SCREEN_HELP: Readonly<Record<string, ScreenHelp>> = {
  pages: {
    summary:
      "Set up the button panels operators and hirers use to control the room — create, rename, reorder and delete pages, each its own mix of scenes, mixer channels and lighting.",
  },
  scenes: {
    summary: "Create and manage scenes — one tap runs a set of actions — and open the editor to change what each one does.",
  },
  rules: {
    summary:
      "Set the schedule that turns things on and off by itself, and see the status Proskenion works out from the current state (the Rules and Derived status tabs).",
  },
  "hirer-access": {
    summary:
      "Control what a hirer's PIN can reach: which pages, how far mixer and lighting levels can go, and the kill switch that locks everything out at once.",
    doc: { id: "hire-handover", heading: "Before a hire" },
  },
  "knx-library": {
    summary: "Manage the KNX group addresses and devices behind house lighting and any other KNX-controlled fittings.",
  },
  lighting: {
    summary: "Configure the lighting rig: the stage plan, fixtures, bars, groups, colour presets and fixture profiles.",
  },
  mixer: {
    // Generic on purpose — the mixer admin screen is under separate, concurrent change.
    summary: "Configure the mixer's channels, outputs and desk scenes.",
  },
  hdmi: {
    summary: "Name the HDMI matrix's inputs and outputs, and group outputs into the destinations an operator picks from.",
  },
  "control-surface": {
    summary: "Configure a connected control surface. This item only appears once one is added on Devices — the configurator itself is not built yet.",
  },
  network: {
    summary: "Change the appliance's IP address, gateway and DNS, and apply them — this briefly disconnects the browser while it takes effect.",
  },
  certificates: {
    summary: "Manage the HTTPS certificate: issue one through Cloudflare or ACME, see renewal history, or fall back to a self-signed certificate.",
  },
  devices: {
    summary: "Add, configure and remove the drivers for every piece of connected hardware — lighting, mixer, projector, HDMI matrix and more.",
  },
  email: {
    summary: "Set the outgoing mail server Proskenion uses to send notifications, and send a test message.",
  },
  backup: {
    summary: "Manage backup destinations and media, back up now, restore from a backup, and manage system images.",
  },
  updates: {
    summary: "Upload and apply application updates, manage operating system slots, and restart, reboot or shut down the appliance.",
  },
  health: {
    summary: "See the appliance's own health in detail: CPU, memory, storage, backup media, and every connected device's status. Restart, reboot or shut down the appliance from the bottom of the page.",
  },
  logs: {
    summary: "Look through the scene execution log, the security event log and the raw system log, and turn on debug logging.",
  },
  users: {
    summary: "Change the admin and operator passwords.",
  },
  help: {
    summary: "The same shortcuts, version, documentation and recovery information as the ? sheet, as an ordinary page.",
  },
};

/** Operator tabs (`web/src/navigation.ts`'s `OPERATOR_TABS`), one summary each. */
export const OPERATOR_SCREEN_HELP: Readonly<Record<string, ScreenHelp>> = {
  pages: {
    summary:
      "The button panels an admin has built for this venue — whatever mix of scenes, mixer channels and lighting the panel was set up with. This is where you land when you sign in.",
    doc: OPERATOR_QUICK_REFERENCE_SCREENS,
  },
  scenes: {
    summary: "A grid of buttons, one per scene. Tap one to run it immediately — there is no confirmation step, so check it's the one you mean first.",
    doc: { id: "operator-quick-reference", heading: "Recalling a scene" },
  },
  mixer: {
    summary: "The virtual channel strips for the mixer: faders, mutes, and the desk-scene band along the top for recalling a full mixer scene.",
    doc: OPERATOR_QUICK_REFERENCE_SCREENS,
  },
  lighting: {
    summary: "Group and individual lighting faders, a master, and the fade time.",
    doc: OPERATOR_QUICK_REFERENCE_SCREENS,
  },
  "stage-plan": {
    summary: "A drawing of the stage showing where each lighting bar and fixture is.",
    doc: OPERATOR_QUICK_REFERENCE_SCREENS,
  },
  video: {
    summary: "Which HDMI source is routed to which output on the matrix.",
    doc: OPERATOR_QUICK_REFERENCE_SCREENS,
  },
  projector: {
    summary: "Power, input, and the projector's own status — warming up, cooling down, and so on.",
    doc: OPERATOR_QUICK_REFERENCE_SCREENS,
  },
};

/** The hirer shell has one screen — their assigned page(s) — not listed in `navigation.ts` since it is not a fixed nav item. */
export const HIRER_SCREEN_HELP: ScreenHelp = {
  summary: "Your assigned page — the same kind of button panel an admin has set up for you, showing only what you're allowed to use.",
};

export interface ResolvedScreen {
  help: ScreenHelp;
}

/**
 * Resolves the current path (and tier) to a screen's help, or `null` when
 * the "On this screen" section has nothing to show: `/login`, `/setup`, an
 * admin path segment with no matching screen, or a tier/path combination
 * routing itself never produces.
 */
export function resolveScreen(tier: Tier, pathname: string): ResolvedScreen | null {
  const adminMatch = /^\/admin\/([^/]+)/.exec(pathname);
  if (adminMatch) {
    if (tier !== "admin") return null;
    const help = ADMIN_SCREEN_HELP[adminMatch[1] ?? ""];
    return help ? { help } : null;
  }

  const appMatch = /^\/app\/([^/]+)/.exec(pathname);
  if (appMatch) {
    if (tier !== "admin" && tier !== "operator") return null;
    const help = OPERATOR_SCREEN_HELP[appMatch[1] ?? ""];
    return help ? { help } : null;
  }

  if (/^\/hire(\/|$)/.test(pathname)) {
    return tier === "hirer" ? { help: HIRER_SCREEN_HELP } : null;
  }

  return null;
}

export interface OnScreenEntry extends HelpEntry {
  id: HelpId;
}

/**
 * Every inline help (ⓘ) trigger rendered under `root`, in document order —
 * the order the controls actually appear in — deduplicated by id (a control
 * that somehow renders twice should not repeat its own explanation). `root`
 * is meant to be the screen's own content (`Shell.tsx`'s `#main`), not the
 * whole document, though nothing here carries a `[data-help-trigger]` inside
 * the sheet itself either way.
 */
export function collectOnScreenEntries(root: ParentNode): OnScreenEntry[] {
  const seen = new Set<string>();
  const entries: OnScreenEntry[] = [];
  const content = HELP_CONTENT as Record<string, HelpEntry>;
  root.querySelectorAll("[data-help-trigger][data-help-id]").forEach((el) => {
    const id = el.getAttribute("data-help-id");
    if (!id || seen.has(id)) return;
    const entry = content[id];
    if (!entry) return; // defensive: every real trigger's id is a valid HelpId, but an attribute is just a string
    seen.add(id);
    entries.push({ id: id as HelpId, term: entry.term, body: entry.body });
  });
  return entries;
}
