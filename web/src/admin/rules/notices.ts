/*
 * Plain-language notices. `SURFACE_NOT_FIRING` is also sent verbatim as
 * `Rule.note` (`proskenion/rules/model.py`), mirrored here so the editor can
 * show the same wording before a rule exists to have a `note` of its own —
 * spec §21.17: "Say so plainly on the form and on the rule's row, so nobody
 * expects one to run."
 */

/** How a schedule behaves when the controller is off and across daylight saving (§8.3). */
export const SCHEDULE_BEHAVIOUR =
  "Runs on Pacific/Auckland time. A time missed while the controller is off is logged and skipped, never run late. When the clocks go forward in September, a time from 02:00 to 02:59 runs once at 03:00; when they go back in April, it runs once, the first time round.";

export const SURFACE_NOT_FIRING =
  "Fires only when a control surface or page button assigned to it is pressed (§7.6), or from Test. Control surfaces arrive in Phase 9.";

export const NOTIFY_IS_LOG_ONLY =
  "This writes an alert to the execution log. Email delivery arrives in a later phase (§11.4) — nothing is sent yet.";

export const BINDING_FORCES_MULTIPLIER =
  "On recall this forces the group's multiplier to 100% and ignores whatever the web interface last left it at. Without that, a group left at 40% would make the next panel press come up dim while the indicator reported “on” — with no way to diagnose it from the panel.";
