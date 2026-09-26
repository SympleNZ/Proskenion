/*
 * Deleting a bar with fixtures on it (spec §21.18 *Bars tab*, §15.9's
 * `bar_id ON DELETE SET NULL`): "asks where to move them... move them with
 * the channel PUTs, then delete the bar. If a move fails partway, report
 * which fixtures moved and do not delete the bar." The sequencing and
 * failure bookkeeping is pulled out of the dialog so it is a function call
 * away from a test rather than only reachable by driving the UI.
 */

export interface MoveFixturesResult {
  /** Fixture ids whose `PUT` succeeded, in the order they were attempted. */
  moved: readonly number[];
  /** Set once every fixture moved and the bar itself was deleted. */
  barDeleted: boolean;
  /** The fixture id and error a failed move stopped on, absent on full success. */
  failure?: { fixtureId: number; error: unknown };
}

/**
 * Moves every id in `fixtureIds` (via `moveFixture`, awaited one at a time —
 * never in parallel, so a failure leaves a known, reportable boundary between
 * what moved and what did not) to `targetBarId` (`null` for unassigned), then
 * calls `deleteBar` only once all of them have succeeded.
 */
export async function moveFixturesAndDeleteBar(
  fixtureIds: readonly number[],
  moveFixture: (fixtureId: number) => Promise<void>,
  deleteBar: () => Promise<void>,
): Promise<MoveFixturesResult> {
  const moved: number[] = [];
  for (const fixtureId of fixtureIds) {
    try {
      await moveFixture(fixtureId);
      moved.push(fixtureId);
    } catch (error) {
      return { moved, barDeleted: false, failure: { fixtureId, error } };
    }
  }
  await deleteBar();
  return { moved, barDeleted: true };
}
