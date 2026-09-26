/*
 * The multi-select **Set level** action's arithmetic (spec §21.12: "Setting a
 * level applies proportionally across the selection", left undefined beyond
 * that sentence).
 *
 * The meaning chosen here — reported to Simon — is the standard
 * lighting-desk sense of "proportional": the control's target value
 * is where the BRIGHTEST fixture in the selection ends up, and every other
 * selected fixture scales by the same ratio, so the selection's relative
 * balance (which fixture is brighter than which, and by how much) is
 * preserved rather than flattened to one identical level. A rig with a wash
 * at 80% and a special at 40% (2:1) set to 100% lands at 100%/50% — still
 * 2:1 — rather than both jumping to 100%. This mirrors "proportional" /
 * "block" fader technique on a real lighting desk, which is the vocabulary
 * §21.4's hardware aesthetic is drawing from elsewhere in this interface.
 *
 * When every selected fixture reads zero there is no ratio to preserve, so
 * the target applies uniformly instead of dividing by zero.
 */

export function proportionalLevels(currentLevels: ReadonlyMap<number, number>, targetLevel: number): Map<number, number> {
  const result = new Map<number, number>();
  const values = [...currentLevels.values()];
  const maxCurrent = values.length > 0 ? Math.max(...values) : 0;
  const clampedTarget = Math.min(100, Math.max(0, targetLevel));

  if (maxCurrent <= 0) {
    for (const id of currentLevels.keys()) result.set(id, clampedTarget);
    return result;
  }

  const ratio = clampedTarget / maxCurrent;
  for (const [id, level] of currentLevels) {
    const scaled = Math.round(level * ratio * 10) / 10;
    result.set(id, Math.min(100, Math.max(0, scaled)));
  }
  return result;
}
