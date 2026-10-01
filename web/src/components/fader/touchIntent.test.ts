import { describe, expect, it } from "vitest";

import { classifyTouch, TOUCH_SLOP_PX } from "./touchIntent";

describe("classifyTouch", () => {
  it("is pending while both axes stay inside the slop", () => {
    expect(classifyTouch(0, 0, "vertical")).toBe("pending");
    expect(classifyTouch(TOUCH_SLOP_PX, -TOUCH_SLOP_PX, "vertical")).toBe("pending");
  });

  it("a vertical fader: vertical past the slop is along, horizontal is across", () => {
    expect(classifyTouch(2, -9, "vertical")).toBe("along");
    expect(classifyTouch(-9, 2, "vertical")).toBe("across");
  });

  it("the larger axis wins when both pass the slop at once; a tie is the fader's", () => {
    expect(classifyTouch(20, 12, "vertical")).toBe("across");
    expect(classifyTouch(12, 20, "vertical")).toBe("along");
    expect(classifyTouch(15, 15, "vertical")).toBe("along");
  });

  it("the horizontal form swaps the axes", () => {
    expect(classifyTouch(9, 2, "horizontal")).toBe("along");
    expect(classifyTouch(2, 9, "horizontal")).toBe("across");
  });
});
