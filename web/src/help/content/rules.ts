/* Rules (spec §21.17, §8). Rules say when; scenes say what. */
import type { HelpEntry } from "../types";

export const rules = {
  "rules.add": {
    term: "Add rule",
    body: "Opens a new rule in the same editor used to edit one.",
  },
  "rules.name": {
    term: "Name",
    body: "What this rule is called in the rules list and its log.",
  },
  "rules.trigger": {
    term: "Trigger",
    body: "What starts this rule: a KNX telegram, a schedule, a control surface press, or a device changing state. The fields below change with this choice.",
  },
  "rules.trigger.knx.address": {
    term: "Address",
    body: "The KNX group address that starts this rule. Only incoming addresses are offered — the rule listens, it does not write.",
  },
  "rules.trigger.knx.match": {
    term: "Match",
    body: "Which telegram values fire the rule. Options are limited by the address's data point type — a switch, for instance, offers on/off rather than a numeric range.",
  },
  "rules.trigger.knx.match-value": {
    term: "Value",
    body: "The value (or, for a range, its lower bound) the telegram must carry to fire the rule.",
  },
  "rules.trigger.knx.match-value-max": {
    term: "Value (upper)",
    body: "The upper bound of the range the telegram's value must fall within to fire the rule.",
  },
  "rules.trigger.knx.debounce": {
    term: "Debounce (ms)",
    body: "A repeat telegram within this window is ignored, so a panel that sends on press and release does not restart a fade. KNX triggers only.",
  },
  "rules.trigger.knx.source": {
    term: "Only from device",
    body: "Optional. The KNX individual address (area.line.device) of the one device whose telegrams fire this rule, e.g. 1.1.26 for the back-of-house panel. Empty means any device. Two panels that send on the same group address can then start different rules.",
  },
  "rules.trigger.schedule.cron": {
    term: "Cron expression",
    body: "Minute, hour, day-of-month, month, day-of-week, evaluated in Pacific/Auckland. A time inside the September daylight-saving gap is skipped; the repeated April hour runs once.",
  },
  "rules.trigger.device-state.device": {
    term: "Device",
    body: "The device whose state this rule watches.",
  },
  "rules.trigger.device-state.state": {
    term: "State",
    body: "The connection status (online, offline) or driver-reported state to watch for.",
  },
  "rules.trigger.device-state.sustained": {
    term: "Sustained for (ms)",
    body: "Optional. The device must stay in this state for this long before the rule fires, so a brief reconnection does not trigger it.",
  },
  "rules.guard.type": {
    term: "Guard",
    body: "An optional extra condition the trigger must also satisfy: a time window, external control being active or inactive, or another device's state. One guard, not a tree — anything more complex belongs in a scene.",
  },
  "rules.guard.time-window.from": {
    term: "From",
    body: "The guard is satisfied from this time.",
  },
  "rules.guard.time-window.to": {
    term: "To",
    body: "The guard is satisfied until this time.",
  },
  "rules.guard.external-control": {
    term: "External control",
    body: "Restricts this rule to firing only while external control is active, or only while it is inactive.",
  },
  "rules.action.type": {
    term: "Action",
    body: "What the rule does when it fires: run a scene, drive a lighting group directly, or log a notification. Anything needing steps or delays is a scene — rules never chain.",
  },
  "rules.action.scene": {
    term: "Scene",
    body: "Which scene this rule runs.",
  },
  "rules.action.lighting-group.group": {
    term: "Group",
    body: "Which lighting group this rule drives directly.",
  },
  "rules.action.lighting-group.on": {
    term: "On level (%)",
    body: "The level this group is set to when the trigger's telegram value is treated as on.",
  },
  "rules.action.lighting-group.off": {
    term: "Off level (%)",
    body: "The level this group is set to when the trigger's telegram value is treated as off.",
  },
  "rules.action.lighting-group.fade": {
    term: "Fade (ms)",
    body: "How long the level takes to reach on or off level.",
  },
  "rules.action.notify.message": {
    term: "Message",
    body: "The text logged when this rule fires. Log only for now — it does not yet send an email or alert.",
  },
  "rules.notes": {
    term: "Notes",
    body: "Anything worth recording about this rule. Shown alongside it, nowhere else.",
  },
  "rules.save": {
    term: "Save",
    body: "Saves this rule.",
  },
  "rules.delete": {
    term: "Delete",
    body: "This cannot be undone. Anything that fires this rule will stop working.",
  },

  // Derived status (§8.6, §21.17 Derived status tab)
  "rules.derived.name": {
    term: "Name",
    body: "What this derived status is called in the list.",
  },
  "rules.derived.address": {
    term: "Address (outgoing, 1-bit)",
    body: "The KNX address this status is written to. Each address can carry only one derived status, and a rule that triggers on it is refused — a status write must never look like a state change to a rule.",
  },
  "rules.derived.reflects": {
    term: "Reflects",
    body: "What this status is derived from: a lighting group reaching a level, a device's state, external control being active, or an HDMI destination showing a particular input.",
  },
  "rules.derived.group": {
    term: "Group",
    body: "Which lighting group this status watches.",
  },
  "rules.derived.level": {
    term: "At level (%)",
    body: "On when every member channel of the group is at this level.",
  },
  "rules.derived.basis": {
    term: "Compare",
    body:
      "Stored level compares each fixture's own fader level, as the spec's §8.6 describes. " +
      "What the room sees compares each fixture's actual output — its level scaled by the Master — so a wall-panel lamp " +
      "follows the stage: with the Master pulled down it reads off even though the faders are up. Use it for panel indicators.",
  },
  "rules.derived.device": {
    term: "Device",
    body: "Which device this status watches.",
  },
  "rules.derived.state": {
    term: "State",
    body: "The state that turns this status on.",
  },
  "rules.derived.hdmi-destination": {
    term: "HDMI destination",
    body: "Which HDMI destination this status watches.",
  },
  "rules.derived.hdmi-input": {
    term: "Shows input",
    body: "On while the destination is showing this input. Off while it shows another, and off while its outputs disagree. One status per input, each on its own feedback address, gives a panel's buttons exactly one lamp lit.",
  },
  "rules.derived.save": {
    term: "Save",
    body: "Saves this derived status. Recomputed from state on every change — not written by whatever fired.",
  },
  "rules.derived.add": {
    term: "Add derived status",
    body: "Opens a new derived status in the same editor used to edit one.",
  },
  "rules.derived.delete": {
    term: "Delete",
    body: "This cannot be undone.",
  },
} as const satisfies Record<string, HelpEntry>;
