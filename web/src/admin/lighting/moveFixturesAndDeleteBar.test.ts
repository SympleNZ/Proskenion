import { describe, expect, it, vi } from "vitest";

import { moveFixturesAndDeleteBar } from "./moveFixturesAndDeleteBar";

describe("moveFixturesAndDeleteBar (§21.18)", () => {
  it("moves every fixture then deletes the bar", async () => {
    const moved: number[] = [];
    const moveFixture = vi.fn(async (id: number) => {
      moved.push(id);
    });
    const deleteBar = vi.fn(async () => undefined);

    const result = await moveFixturesAndDeleteBar([1, 2, 3], moveFixture, deleteBar);

    expect(moved).toEqual([1, 2, 3]);
    expect(deleteBar).toHaveBeenCalledOnce();
    expect(result).toEqual({ moved: [1, 2, 3], barDeleted: true });
  });

  it("stops at the first failure, reports which fixtures moved, and never deletes the bar", async () => {
    const error = new Error("network");
    const moveFixture = vi.fn(async (id: number) => {
      if (id === 2) throw error;
    });
    const deleteBar = vi.fn(async () => undefined);

    const result = await moveFixturesAndDeleteBar([1, 2, 3], moveFixture, deleteBar);

    expect(result.moved).toEqual([1]);
    expect(result.barDeleted).toBe(false);
    expect(result.failure).toEqual({ fixtureId: 2, error });
    expect(deleteBar).not.toHaveBeenCalled();
    // Fixture 3 was never attempted — the run stops at the first failure.
    expect(moveFixture).toHaveBeenCalledTimes(2);
  });

  it("deletes the bar immediately when it has no fixtures to move", async () => {
    const moveFixture = vi.fn(async () => undefined);
    const deleteBar = vi.fn(async () => undefined);

    const result = await moveFixturesAndDeleteBar([], moveFixture, deleteBar);

    expect(moveFixture).not.toHaveBeenCalled();
    expect(deleteBar).toHaveBeenCalledOnce();
    expect(result).toEqual({ moved: [], barDeleted: true });
  });
});
