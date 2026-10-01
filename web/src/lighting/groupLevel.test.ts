/*
 * What a group fader shows (owner decision 2026-09-30): the level its
 * members share — allowing for each member's own range — or the highest
 * member level, marked mixed.
 */
import { describe, expect, it } from "vitest";

import { groupLevel, type GroupMember } from "./groupLevel";

const member = (id: number, min_value = 0, max_value = 100): GroupMember => ({ id, min_value, max_value });

describe("groupLevel", () => {
  it("members at one level: that level, not mixed", () => {
    expect(groupLevel([member(1), member(2)], [60, 60])).toEqual({ value: 60, mixed: false });
  });

  it("members that differ: the highest, mixed", () => {
    expect(groupLevel([member(1), member(2), member(3)], [60, 80, 20])).toEqual({ value: 80, mixed: true });
  });

  it("a member held below the group by its max_value is not mixed", () => {
    expect(groupLevel([member(1), member(2, 0, 80)], [90, 80])).toEqual({ value: 90, mixed: false });
  });

  it("a member held above the group by its floor is not mixed, and the group reads its own level", () => {
    expect(groupLevel([member(1), member(2, 10), member(3)], [0, 10, 0])).toEqual({ value: 0, mixed: false });
  });

  it("nothing stored yet reads as 0; no members is 0", () => {
    expect(groupLevel([member(1), member(2)], [null, null])).toEqual({ value: 0, mixed: false });
    expect(groupLevel([], [])).toEqual({ value: 0, mixed: false });
  });

  it("levels within a twentieth of a percent are the same level", () => {
    expect(groupLevel([member(1), member(2)], [50, 50.04])).toEqual({ value: 50.04, mixed: false });
  });
});
