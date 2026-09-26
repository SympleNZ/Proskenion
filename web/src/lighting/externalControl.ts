/*
 * What goes read-only under external control (spec §7.2.3's behaviour
 * matrix, §7.2.7, §9.4, §9.5, §21.11). Pure and store-agnostic, so every
 * control that changes lighting — the Lighting view's faders, the stage plan,
 * the stage banks — asks here rather than re-deriving the rule.
 *
 * §7.2.3's matrix, the row this mirrors:
 *
 *   | Condition                          | DMX pass                        | KNX pass
 *   | External control active (§7.2.7)   | Suspended — nothing written     | Runs normally. House lighting is
 *   |                                     |                                  | never gated by DMX state
 *
 * External control suspends only the DMX pass. The KNX pass — house
 * lighting — runs normally throughout, so a `knx_dimmer` channel has
 * nothing suspended and nothing to observe (§7.2.7's `observed` carries only
 * channels patched to a DMX fixture): it stays interactive, live, showing
 * the controller's own model and its ghost mark, exactly as it does off.
 * Only a `dmx` channel goes read-only, and only once external control is
 * active at all — detected or manual, both count (§7.2.7 "behaviour while
 * active").
 *
 * The master and the group faders follow from the same row. Both scale stage
 * (DMX) output only; KNX house dimmers are outside them (§9.4, §9.5). So the
 * master acts only through the pass that external control suspends, and it
 * goes read-only with the DMX faders (§21.11: "faders … show observed
 * levels, live and read-only"). A group is read-only when it contains a DMX
 * fixture. **A group containing only KNX dimmers is unaffected by external
 * control**: it holds no stage lighting, so its fader and a stage bank that
 * recalls it stay live, as house lighting always does. The controller agrees:
 * a binding on such a group still fires while a desk is connected, and its
 * panel status is not written 0. An empty group counts the same way, since
 * it holds no stage lighting either.
 */
import type { ExternalControl } from "@/live/store";

import type { LightingChannelType } from "./types";

/** A lighting channel as far as external control cares: what kind of output drives it. */
export interface ExternalControlChannel {
  readonly type: LightingChannelType;
}

export function isReadOnlyUnderExternalControl(channel: ExternalControlChannel, externalControl: ExternalControl): boolean {
  return channel.type === "dmx" && externalControl !== "off";
}

/** The master dimmer scales stage (DMX) output only (§9.5), which external control suspends. */
export function isMasterReadOnlyUnderExternalControl(externalControl: ExternalControl): boolean {
  return externalControl !== "off";
}

/**
 * A group's level controls — its fader, and a stage bank recalling it — are
 * read-only once any member is. `members` is `undefined` while the group's
 * membership is not yet known; the group is then treated as holding stage
 * lighting, since offering a control the controller would refuse is worse
 * than withholding one for the moment it takes the configuration to load.
 */
export function isGroupReadOnlyUnderExternalControl(
  members: readonly ExternalControlChannel[] | undefined,
  externalControl: ExternalControl,
): boolean {
  if (externalControl === "off") return false;
  if (members === undefined) return true;
  return members.some((member) => isReadOnlyUnderExternalControl(member, externalControl));
}

/** What the stage plan's multi-select Set level may set, and what it must leave alone. */
export interface SetLevelTargets<T extends ExternalControlChannel> {
  /** The fixtures the Set level applies to, in selection order. */
  readonly settable: readonly T[];
  /** Stage (DMX) fixtures left out because external control is active. */
  readonly skipped: readonly T[];
}

/**
 * The stage plan's multi-select **Set level** (§21.12) under external control
 * (§21.11: the stage plan is read-only; house lighting is unaffected). It
 * follows the per-channel rule above, fixture by fixture: a DMX fixture is
 * read-only, so it is skipped; a KNX house dimmer stays live, so it is set.
 *
 * A selection of stage fixtures only therefore has nothing to set, and the
 * control is unavailable. A selection of house dimmers only is set as it is
 * with external control off. **A mixed selection sets its house dimmers and
 * skips its stage fixtures**, rather than refusing the whole action: the
 * house dimmers are as settable as they would be one at a time from their own
 * faders, and refusing them because a stage fixture shares the selection
 * would gate house lighting on DMX state, which §7.2.3 rules out. The caller
 * says which fixtures were skipped, so the partial result is never silent.
 */
export function setLevelTargetsUnderExternalControl<T extends ExternalControlChannel>(
  selection: readonly T[],
  externalControl: ExternalControl,
): SetLevelTargets<T> {
  const settable: T[] = [];
  const skipped: T[] = [];
  for (const fixture of selection) {
    (isReadOnlyUnderExternalControl(fixture, externalControl) ? skipped : settable).push(fixture);
  }
  return { settable, skipped };
}
