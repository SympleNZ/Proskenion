import { describe, expect, it } from "vitest";

import { conflictKey, conflictMessage } from "./conflicts";
import type { HirerConflict } from "./types";

describe("conflictMessage", () => {
  it("a desk scene above the ceiling, once observed", () => {
    const conflict: HirerConflict = {
      channel_id: 5,
      channel_name: "Wireless Mic 1",
      ceiling_db: -5,
      level_db: -1,
      source: { kind: "desk_scene", desk_scene_id: 2, name: "Band" },
    };
    const message = conflictMessage(conflict);
    expect(message).toContain('“Band” recalls Wireless Mic 1 at -1.0 dB');
    expect(message).toContain("-5.0 dB ceiling");
    expect(message).toContain("clamped immediately after the scene is recalled");
  });

  it("a desk scene never recalled reads 'not yet checked' rather than showing a level (observed: false)", () => {
    const conflict: HirerConflict = {
      channel_id: 5,
      channel_name: "Wireless Mic 1",
      ceiling_db: -5,
      level_db: null,
      observed: false,
      source: { kind: "desk_scene", desk_scene_id: 3, name: "Band 2" },
    };
    const message = conflictMessage(conflict);
    expect(message).toContain("never been recalled");
    expect(message).toContain("not yet checked");
    expect(message).toContain("recall it once to check");
    expect(message).not.toMatch(/null/);
  });

  it("a scene action above the ceiling", () => {
    const conflict: HirerConflict = {
      channel_id: 9,
      channel_name: "Main",
      ceiling_db: -3,
      level_db: 0,
      source: { kind: "scene_action", scene_id: 4, action_id: 11, name: "Interval" },
    };
    const message = conflictMessage(conflict);
    expect(message).toContain('“Interval” sets Main to 0.0 dB');
    expect(message).toContain("-3.0 dB ceiling");
    expect(message).toContain("when this scene runs");
  });
});

describe("conflictKey", () => {
  it("distinguishes two conflicts on the same channel by their source", () => {
    const deskScene: HirerConflict = {
      channel_id: 5,
      channel_name: "Wireless Mic 1",
      ceiling_db: -5,
      level_db: -1,
      source: { kind: "desk_scene", desk_scene_id: 2, name: "Band" },
    };
    const sceneAction: HirerConflict = {
      channel_id: 5,
      channel_name: "Wireless Mic 1",
      ceiling_db: -5,
      level_db: 0,
      source: { kind: "scene_action", scene_id: 4, action_id: 11, name: "Interval" },
    };
    expect(conflictKey(deskScene)).not.toBe(conflictKey(sceneAction));
  });
});
