/*
 * The vertical touch fader (spec §10.3, §21.2, §24.2, §22.3). Acceptance
 * criteria covered here: gesture arbitration's pointer half (the store half
 * is `lighting/ChannelFader.test.tsx`), two pointers driving two faders
 * independently, keyboard exactly per §24.2 including the corrected
 * aria-valuenow/aria-valuetext example, and the ghost mark's rendering.
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { linearLightingScale } from "./FaderScale";
import { FaderStrip } from "./FaderStrip";

// jsdom lays out nothing by default; every drag test needs a real rect to
// convert a pointer's clientY into a position.
const RECT = { top: 0, bottom: 200, left: 0, right: 44, width: 44, height: 200, x: 0, y: 0, toJSON: () => ({}) };
beforeEach(() => {
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue(RECT as DOMRect);
});

const scale = linearLightingScale();

function pointerDown(el: Element, pointerId: number, clientY: number): void {
  fireEvent.pointerDown(el, { pointerId, clientY, button: 0 });
}
function pointerMove(el: Element, pointerId: number, clientY: number): void {
  fireEvent.pointerMove(el, { pointerId, clientY });
}
function pointerUp(el: Element, pointerId: number, clientY: number): void {
  fireEvent.pointerUp(el, { pointerId, clientY });
}

describe("FaderStrip — ARIA and keyboard (§24.2)", () => {
  it("carries position in aria-valuenow and the scale's readable value in aria-valuetext", () => {
    render(<FaderStrip label="Row 1" value={82.5} scale={scale} onChange={vi.fn()} />);
    const slider = screen.getByRole("slider", { name: "Row 1 fader" });
    expect(slider).toHaveAttribute("aria-valuemin", "0");
    expect(slider).toHaveAttribute("aria-valuemax", "1000");
    expect(slider).toHaveAttribute("aria-valuenow", "825"); // position 0.825 × 1000
    expect(slider).toHaveAttribute("aria-valuetext", "82.5%");
  });

  it("prefers a scale's spokenFormat for aria-valuetext over the printed readout (§24.2)", () => {
    const spoken = { ...linearLightingScale(), spokenFormat: (value: number | null) => `${value} percent, spoken` };
    render(<FaderStrip label="Row 1" value={82.5} scale={spoken} onChange={vi.fn()} />);
    const slider = screen.getByRole("slider", { name: "Row 1 fader" });
    expect(slider).toHaveAttribute("aria-valuetext", "82.5 percent, spoken");
    expect(screen.getByText("82.5%")).toBeInTheDocument(); // the printed readout is unaffected
  });

  it("Arrow Up/Down step ±1%, Page Up/Down ±10%, Home/End go to the ends (§24.2)", () => {
    const onChange = vi.fn();
    render(<FaderStrip label="Fixture" value={50} scale={scale} onChange={onChange} />);
    const slider = screen.getByRole("slider", { name: "Fixture fader" });
    slider.focus();

    fireEvent.keyDown(slider, { key: "ArrowUp" });
    expect(onChange).toHaveBeenLastCalledWith(51);
    fireEvent.keyDown(slider, { key: "ArrowDown" });
    expect(onChange).toHaveBeenLastCalledWith(49);
    fireEvent.keyDown(slider, { key: "PageUp" });
    expect(onChange).toHaveBeenLastCalledWith(60);
    fireEvent.keyDown(slider, { key: "PageDown" });
    expect(onChange).toHaveBeenLastCalledWith(40);
    fireEvent.keyDown(slider, { key: "Home" });
    expect(onChange).toHaveBeenLastCalledWith(0);
    fireEvent.keyDown(slider, { key: "End" });
    expect(onChange).toHaveBeenLastCalledWith(100);
  });

  it("ignores keys outside the §24.2 vocabulary", () => {
    const onChange = vi.fn();
    render(<FaderStrip label="Fixture" value={50} scale={scale} onChange={onChange} />);
    fireEvent.keyDown(screen.getByRole("slider"), { key: "a" });
    expect(onChange).not.toHaveBeenCalled();
  });

  it("is not focusable and ignores keys once disabled", () => {
    const onChange = vi.fn();
    render(<FaderStrip label="Fixture" value={50} scale={scale} onChange={onChange} disabled />);
    const slider = screen.getByRole("slider");
    expect(slider).toHaveAttribute("tabindex", "-1");
    expect(slider).toHaveAttribute("aria-disabled", "true");
    fireEvent.keyDown(slider, { key: "ArrowUp" });
    expect(onChange).not.toHaveBeenCalled();
  });
});

describe("FaderStrip — pointer drag", () => {
  it("jumps to the tapped position on pointer down and flushes on release", () => {
    const onChange = vi.fn();
    const onGestureStart = vi.fn();
    const onGestureEnd = vi.fn();
    render(
      <FaderStrip label="Fixture" value={0} scale={scale} onChange={onChange} onGestureStart={onGestureStart} onGestureEnd={onGestureEnd} />,
    );
    const slider = screen.getByRole("slider");

    pointerDown(slider, 1, 100); // middle of a 200-tall track → position 0.5
    expect(onGestureStart).toHaveBeenCalledTimes(1);
    expect(onChange).toHaveBeenCalledWith(50);

    pointerUp(slider, 1, 0); // released at the top → position 1
    expect(onChange).toHaveBeenLastCalledWith(100);
    expect(onGestureEnd).toHaveBeenCalledWith(100);
  });

  it("throttles onChange during the drag but always flushes the final value on release (§21.2 ~30/s)", () => {
    const times = [0, 5, 10, 15, 20, 60]; // most moves land inside one 33 ms window
    let call = 0;
    vi.spyOn(performance, "now").mockImplementation(() => times[Math.min(call++, times.length - 1)] ?? 0);

    const onChange = vi.fn();
    render(<FaderStrip label="Fixture" value={0} scale={scale} onChange={onChange} />);
    const slider = screen.getByRole("slider");

    pointerDown(slider, 1, 200); // t=0, flushed unconditionally → position 0
    pointerMove(slider, 1, 190); // t=5, inside the throttle window → skipped
    pointerMove(slider, 1, 180); // t=10, inside the window → skipped
    pointerUp(slider, 1, 170); // release → always flushed regardless of the window

    expect(onChange).toHaveBeenCalledTimes(2); // the initial jump and the final release
  });

  it("a cancelled pointer ends at the last position it reached, never at the cancel event's own coordinates", () => {
    // Chrome sends pointercancel with clientX/clientY of 0 — when a native
    // drag of selected text takes over the mouse, or the browser claims a
    // touch. Read as a position, clientY 0 is above the top of travel: the
    // fader jumped to its maximum (+10 dB on the CQ law) and sent it.
    let clock = 0;
    vi.spyOn(performance, "now").mockImplementation(() => (clock += 40));
    const onChange = vi.fn();
    const onGestureEnd = vi.fn();
    render(<FaderStrip label="Fixture" value={0} scale={scale} onChange={onChange} onGestureEnd={onGestureEnd} />);
    const slider = screen.getByRole("slider");

    pointerDown(slider, 1, 100); // position 0.5
    pointerMove(slider, 1, 60); // position 0.7
    fireEvent.pointerCancel(slider, { pointerId: 1, clientY: 0 });

    expect(onChange).toHaveBeenLastCalledWith(70);
    expect(onChange).not.toHaveBeenCalledWith(100);
    expect(onGestureEnd).toHaveBeenCalledWith(70);
    // The gesture is over: a stray move afterwards is not a drag.
    onChange.mockClear();
    pointerMove(slider, 1, 0);
    expect(onChange).not.toHaveBeenCalled();
  });

  it("stops the browser starting a text selection or a native drag from the fader (the cause of the cancel above)", () => {
    render(<FaderStrip label="Fixture" value={0} scale={scale} onChange={vi.fn()} />);
    const slider = screen.getByRole("slider");
    const down = new PointerEvent("pointerdown", { pointerId: 1, clientY: 100, bubbles: true, cancelable: true });
    slider.dispatchEvent(down);
    expect(down.defaultPrevented).toBe(true);
    const drag = new Event("dragstart", { bubbles: true, cancelable: true });
    slider.dispatchEvent(drag);
    expect(drag.defaultPrevented).toBe(true);
  });

  it("ignores a second pointer while the first is still down on the same fader", () => {
    const onChange = vi.fn();
    render(<FaderStrip label="Fixture" value={0} scale={scale} onChange={onChange} />);
    const slider = screen.getByRole("slider");
    pointerDown(slider, 1, 100);
    onChange.mockClear();
    pointerMove(slider, 2, 0); // a different pointer id — not the one that captured this fader
    expect(onChange).not.toHaveBeenCalled();
  });

  it("does nothing on pointer down while disabled or read-only", () => {
    const onChange = vi.fn();
    const { rerender } = render(<FaderStrip label="Fixture" value={50} scale={scale} onChange={onChange} disabled />);
    pointerDown(screen.getByRole("slider"), 1, 0);
    expect(onChange).not.toHaveBeenCalled();

    rerender(<FaderStrip label="Fixture" value={50} scale={scale} onChange={onChange} readOnly />);
    pointerDown(screen.getByRole("slider"), 1, 0);
    expect(onChange).not.toHaveBeenCalled();
  });

  it("is disabled under a critical scene even when `disabled` is not passed — the write would be rejected outright", () => {
    const onChange = vi.fn();
    render(<FaderStrip label="Fixture" value={50} scale={scale} onChange={onChange} sceneRing="critical" />);
    const slider = screen.getByRole("slider");
    expect(slider).toHaveAttribute("aria-disabled", "true");
    pointerDown(slider, 1, 0);
    expect(onChange).not.toHaveBeenCalled();
  });
});

describe("FaderStrip — two pointers drive two faders independently (§10.3, §22.3)", () => {
  it("each fader tracks only its own pointer", () => {
    let clock = 0;
    vi.spyOn(performance, "now").mockImplementation(() => (clock += 40)); // outside every throttle window

    const onChangeA = vi.fn();
    const onChangeB = vi.fn();
    render(
      <>
        <FaderStrip label="Fixture A" value={0} scale={scale} onChange={onChangeA} />
        <FaderStrip label="Fixture B" value={0} scale={scale} onChange={onChangeB} />
      </>,
    );
    const [sliderA, sliderB] = screen.getAllByRole("slider");
    if (!sliderA || !sliderB) throw new Error("expected two sliders");

    pointerDown(sliderA, 11, 50); // A: position 0.75
    pointerDown(sliderB, 22, 150); // B: position 0.25, under a different finger

    expect(onChangeA).toHaveBeenLastCalledWith(75);
    expect(onChangeB).toHaveBeenLastCalledWith(25);

    pointerMove(sliderA, 11, 0); // only A's pointer moves
    expect(onChangeA).toHaveBeenLastCalledWith(100);
    expect(onChangeB).toHaveBeenLastCalledWith(25); // B is untouched
  });
});

describe("FaderStrip — ghost mark (§9.4)", () => {
  it("shows the composited value beside the readout only when it differs from the set value", () => {
    const { rerender } = render(<FaderStrip label="Fixture" value={85} scale={scale} onChange={vi.fn()} ghostValue={55} />);
    expect(screen.getByText("85.0%")).toBeInTheDocument();
    expect(screen.getByText("→55.0%")).toBeInTheDocument();

    rerender(<FaderStrip label="Fixture" value={85} scale={scale} onChange={vi.fn()} ghostValue={85} />);
    expect(screen.queryByText(/→/)).not.toBeInTheDocument();
  });

  it("renders no ghost mark at all when ghostValue is null", () => {
    const { container } = render(<FaderStrip label="Fixture" value={85} scale={scale} onChange={vi.fn()} ghostValue={null} />);
    expect(container.querySelector(".fader-ghost-mark")).toBeNull();
  });
});

describe("FaderStrip — ceiling (§18 Q4, Q9, Q10 P5-T11)", () => {
  it("draws a mark on the track at the ceiling position", () => {
    const { container } = render(<FaderStrip label="Fixture" value={50} scale={scale} onChange={vi.fn()} ceiling={80} />);
    const mark = container.querySelector<HTMLElement>(".fader-ceiling-mark");
    expect(mark).not.toBeNull();
    expect(mark?.style.bottom).toBe("80%"); // linear 0–100 scale: 80% of travel
  });

  it("draws no ceiling mark when none is set", () => {
    const { container } = render(<FaderStrip label="Fixture" value={50} scale={scale} onChange={vi.fn()} ceiling={null} />);
    expect(container.querySelector(".fader-ceiling-mark")).toBeNull();
  });

  it("stops a drag at the ceiling — a pointer past it never sends a value above it", () => {
    const onChange = vi.fn();
    render(<FaderStrip label="Fixture" value={0} scale={scale} onChange={onChange} ceiling={80} />);
    const slider = screen.getByRole("slider");

    pointerDown(slider, 1, 0); // the very top of a 200-tall track → position 1.0, uncapped
    expect(onChange).toHaveBeenLastCalledWith(80);

    pointerMove(slider, 1, -50); // even further past the top
    expect(onChange).toHaveBeenLastCalledWith(80);

    pointerUp(slider, 1, 0);
    expect(onChange).toHaveBeenLastCalledWith(80);
  });

  it("lets a drag move freely below the ceiling", () => {
    const onChange = vi.fn();
    render(<FaderStrip label="Fixture" value={0} scale={scale} onChange={onChange} ceiling={80} />);
    pointerDown(screen.getByRole("slider"), 1, 140); // position 0.3, well under the ceiling
    expect(onChange).toHaveBeenLastCalledWith(30);
  });

  it("stops the keyboard ±1 step at the ceiling", () => {
    const onChange = vi.fn();
    render(<FaderStrip label="Fixture" value={79.5} scale={scale} onChange={onChange} ceiling={80} />);
    const slider = screen.getByRole("slider");
    slider.focus();
    fireEvent.keyDown(slider, { key: "ArrowUp" }); // one full %+ step would overshoot the ceiling
    expect(onChange).toHaveBeenLastCalledWith(80);
    fireEvent.keyDown(slider, { key: "ArrowUp" }); // already at the ceiling — holds, does not nack-worthy overshoot
    expect(onChange).toHaveBeenLastCalledWith(80);
  });

  it("stops Page Up and End at the ceiling too", () => {
    const onChange = vi.fn();
    render(<FaderStrip label="Fixture" value={50} scale={scale} onChange={onChange} ceiling={80} />);
    const slider = screen.getByRole("slider");
    slider.focus();
    fireEvent.keyDown(slider, { key: "PageUp" }); // +10 would land on 60, still under the ceiling
    expect(onChange).toHaveBeenLastCalledWith(60);
    fireEvent.keyDown(slider, { key: "End" }); // the top of travel is now the ceiling, not the scale's own max
    expect(onChange).toHaveBeenLastCalledWith(80);
  });

  it("never clamps travel below the ceiling — Home and Arrow Down are unaffected", () => {
    const onChange = vi.fn();
    render(<FaderStrip label="Fixture" value={50} scale={scale} onChange={onChange} ceiling={80} />);
    const slider = screen.getByRole("slider");
    slider.focus();
    fireEvent.keyDown(slider, { key: "ArrowDown" });
    expect(onChange).toHaveBeenLastCalledWith(49);
    fireEvent.keyDown(slider, { key: "Home" });
    expect(onChange).toHaveBeenLastCalledWith(0);
  });

  it("caps aria-valuemax at the ceiling's own position, not the full scale", () => {
    render(<FaderStrip label="Fixture" value={50} scale={scale} onChange={vi.fn()} ceiling={80} />);
    expect(screen.getByRole("slider")).toHaveAttribute("aria-valuemax", "800"); // 80% of 1000
  });
});

describe("FaderStrip — the card (§21.5) and the horizontal form", () => {
  it("renders name, sub-label, readout below the fader, and the foot slot beneath the readout", () => {
    const { container } = render(
      <FaderStrip label="Row 1" sublabel="group" value={40} scale={scale} onChange={vi.fn()} readoutNote="note">
        <button type="button">Bump</button>
      </FaderStrip>,
    );
    const parts = [...container.querySelectorAll(".fader-strip > div")].map((el) => el.className);
    // Head, zone, readout, foot — in that order, so the knob's zone can never reach the readout or the button.
    expect(parts.filter((c) => c !== "fader-strip-accent")).toEqual(["fader-strip-head", "fader-zone", "fader-readout", "fader-strip-foot"]);
    expect(screen.getByText("group")).toBeInTheDocument();
    expect(screen.getByText("note")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Bump" }).closest(".fader-strip-foot")).not.toBeNull();
  });

  it("has a meter slot only when given one", () => {
    const { container, rerender } = render(<FaderStrip label="Ch" value={0} scale={scale} onChange={vi.fn()} />);
    expect(container.querySelector(".fader-meter-slot")).toBeNull();
    rerender(<FaderStrip label="Ch" value={0} scale={scale} onChange={vi.fn()} meter={<span>m</span>} />);
    expect(container.querySelector(".fader-meter-slot")).not.toBeNull();
  });

  it("the horizontal form maps the pointer's x, and Arrow Right/Left step it", () => {
    const onChange = vi.fn();
    render(<FaderStrip label="Master" value={50} scale={scale} onChange={onChange} orientation="horizontal" />);
    const slider = screen.getByRole("slider", { name: "Master fader" });
    expect(slider).toHaveAttribute("aria-orientation", "horizontal");
    fireEvent.pointerDown(slider, { pointerId: 1, clientX: 11, clientY: 0 }); // 11 of a 44-wide rect → 25%
    expect(onChange).toHaveBeenLastCalledWith(25);
    fireEvent.pointerUp(slider, { pointerId: 1, clientX: 33, clientY: 0 });
    expect(onChange).toHaveBeenLastCalledWith(75);
    fireEvent.keyDown(slider, { key: "ArrowRight" });
    expect(onChange).toHaveBeenLastCalledWith(51);
    fireEvent.keyDown(slider, { key: "ArrowLeft" });
    expect(onChange).toHaveBeenLastCalledWith(49);
  });

  it("a vertical fader ignores Arrow Left/Right, as §24.2 lists only Up/Down for it", () => {
    const onChange = vi.fn();
    render(<FaderStrip label="Ch" value={50} scale={scale} onChange={onChange} />);
    fireEvent.keyDown(screen.getByRole("slider"), { key: "ArrowRight" });
    expect(onChange).not.toHaveBeenCalled();
  });
});

describe("FaderStrip — muted and scene rings", () => {
  it("marks the track muted", () => {
    const { container } = render(<FaderStrip label="Fixture" value={50} scale={scale} onChange={vi.fn()} muted />);
    expect(container.querySelector('[data-muted="true"]')).not.toBeNull();
  });

  it("shows a striped pattern when muted, not just reduced opacity (§24.1)", () => {
    // jsdom does not compute styles from a stylesheet, so the pattern is
    // checked directly in the source CSS rather than a rendered element.
    const here = dirname(fileURLToPath(import.meta.url));
    const css = readFileSync(join(here, "..", "..", "styles", "components.css"), "utf8");
    const rule = /\.fader-track\[data-muted="true"\]\s*\.fader-cover\s*\{([^}]*)\}/.exec(css);
    expect(rule, "no CSS rule found for the muted fader's cover").not.toBeNull();
    expect(rule![1]).toMatch(/repeating-linear-gradient/);
    expect(rule![1]).toMatch(/var\(--color-fader-muted\)/);
  });

  it("carries the scene ring as a data attribute for the two states (§10.6)", () => {
    const { rerender } = render(<FaderStrip label="Fixture" value={50} scale={scale} onChange={vi.fn()} sceneRing="normal" />);
    expect(screen.getByRole("slider")).toHaveAttribute("data-scene-ring", "normal");
    rerender(<FaderStrip label="Fixture" value={50} scale={scale} onChange={vi.fn()} sceneRing="critical" />);
    expect(screen.getByRole("slider")).toHaveAttribute("data-scene-ring", "critical");
  });
});

/*
 * Touch in a sideways-scrolling row (owner's request, 30 Sep 2026): a touch
 * on the track waits to learn its direction before it moves the level; a
 * touch on the thumb grabs at once; a sideways swipe never changes the level.
 * The travel is 0–200 px tall (RECT above); the thumb is drawn 32 px tall,
 * centred on value 20's position, so its rect is laid out here from that.
 */
describe("FaderStrip — touch in a scrolling row", () => {
  const THUMB_HALF = 16;
  const THUMB_CENTRE_AT_20 = 160; // value 20 → position 0.2 → 200 − 40

  beforeEach(() => {
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      if (this.classList.contains("fader-thumb")) {
        const top = THUMB_CENTRE_AT_20 - THUMB_HALF;
        return { ...RECT, top, bottom: top + 2 * THUMB_HALF, height: 2 * THUMB_HALF, y: top } as DOMRect;
      }
      return RECT as DOMRect;
    });
    // Every move lands outside the ~30/s window, so each one is sent.
    let clock = 0;
    vi.spyOn(performance, "now").mockImplementation(() => (clock += 40));
  });

  function touch(kind: "Down" | "Move" | "Up" | "Cancel", el: Element, pointerId: number, clientX: number, clientY: number): void {
    const init = { pointerId, pointerType: "touch", clientX, clientY, button: 0 };
    if (kind === "Down") fireEvent.pointerDown(el, init);
    else if (kind === "Move") fireEvent.pointerMove(el, init);
    else if (kind === "Up") fireEvent.pointerUp(el, init);
    else fireEvent.pointerCancel(el, init);
  }

  function setup(extra: { ceiling?: number; readOnly?: boolean } = {}) {
    const onChange = vi.fn();
    const onGestureStart = vi.fn();
    const onGestureEnd = vi.fn();
    render(
      <FaderStrip
        label="Fixture"
        value={20}
        scale={scale}
        onChange={onChange}
        onGestureStart={onGestureStart}
        onGestureEnd={onGestureEnd}
        {...extra}
      />,
    );
    return { slider: screen.getByRole("slider"), onChange, onGestureStart, onGestureEnd };
  }

  it("a tap on the track does nothing until it lifts, then jumps to where it lifted", () => {
    const { slider, onChange, onGestureStart, onGestureEnd } = setup();
    touch("Down", slider, 1, 20, 100);
    expect(onGestureStart).not.toHaveBeenCalled();
    expect(onChange).not.toHaveBeenCalled();
    expect(slider).toHaveAttribute("aria-valuenow", "200"); // the thumb has not moved

    touch("Move", slider, 1, 23, 104); // a wobble inside the slop
    expect(onChange).not.toHaveBeenCalled();

    touch("Up", slider, 1, 23, 104);
    expect(onGestureStart).toHaveBeenCalledTimes(1);
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange).toHaveBeenCalledWith(48);
    expect(onGestureEnd).toHaveBeenCalledWith(48);
  });

  it("a vertical drag from the track jumps once it is known to be one, then tracks the finger", () => {
    const { slider, onChange, onGestureStart, onGestureEnd } = setup();
    touch("Down", slider, 1, 20, 100);
    touch("Move", slider, 1, 21, 95); // inside the slop: still undecided
    expect(onChange).not.toHaveBeenCalled();

    touch("Move", slider, 1, 22, 90); // 10 px along the fader, 2 across: a fader drag
    expect(onGestureStart).toHaveBeenCalledTimes(1);
    expect(onChange).toHaveBeenLastCalledWith(55);

    touch("Move", slider, 1, 30, 60); // sideways drift after that is still the fader's
    expect(onChange).toHaveBeenLastCalledWith(70);
    touch("Up", slider, 1, 30, 40);
    expect(onChange).toHaveBeenLastCalledWith(80);
    expect(onGestureEnd).toHaveBeenCalledWith(80);
  });

  it("a sideways swipe from the track never changes the level, whether the browser takes it or not", () => {
    const { slider, onChange, onGestureStart, onGestureEnd } = setup();
    touch("Down", slider, 1, 20, 100);
    touch("Move", slider, 1, 32, 102); // 12 px across, 2 along: the row's scroll
    touch("Move", slider, 1, 60, 40); // and nothing after that is a fader move
    expect(slider).toHaveAttribute("aria-valuenow", "200");
    // The browser claims the pan: a cancel with Chrome's zeroed coordinates.
    touch("Cancel", slider, 1, 0, 0);
    expect(onChange).not.toHaveBeenCalled();
    expect(onGestureStart).not.toHaveBeenCalled();
    expect(onGestureEnd).not.toHaveBeenCalled();

    // A row with nowhere to scroll: no cancel, the finger just lifts.
    touch("Down", slider, 2, 20, 100);
    touch("Move", slider, 2, 5, 101);
    touch("Up", slider, 2, 5, 101);
    expect(onChange).not.toHaveBeenCalled();
  });

  it("a cancel while the direction is still unknown drops the deferred jump", () => {
    const { slider, onChange, onGestureStart } = setup();
    touch("Down", slider, 1, 20, 100);
    touch("Cancel", slider, 1, 0, 0);
    touch("Up", slider, 1, 20, 100); // a stray release after the cancel is not a tap
    touch("Move", slider, 1, 20, 50);
    expect(onChange).not.toHaveBeenCalled();
    expect(onGestureStart).not.toHaveBeenCalled();
  });

  it("a touch on the thumb grabs at once, without jumping, and drags from where it was held", () => {
    const { slider, onChange, onGestureStart, onGestureEnd } = setup();
    touch("Down", slider, 1, 20, THUMB_CENTRE_AT_20 + 6); // low on the thumb
    expect(onGestureStart).toHaveBeenCalledTimes(1);
    expect(onChange).not.toHaveBeenCalled(); // grabbed, not moved
    expect(slider).toHaveAttribute("data-dragging", "true");
    expect(slider).toHaveAttribute("aria-valuenow", "200");

    touch("Move", slider, 1, 20, THUMB_CENTRE_AT_20 + 6 - 20); // up 20 px = 10 %
    expect(onChange).toHaveBeenLastCalledWith(30);
    touch("Up", slider, 1, 20, THUMB_CENTRE_AT_20 + 6 - 40);
    expect(onChange).toHaveBeenLastCalledWith(40);
    expect(onGestureEnd).toHaveBeenCalledWith(40);
  });

  it("a thumb held and lifted without moving sends nothing", () => {
    const { slider, onChange, onGestureEnd } = setup();
    touch("Down", slider, 1, 20, THUMB_CENTRE_AT_20);
    touch("Up", slider, 1, 20, THUMB_CENTRE_AT_20 + 3);
    expect(onChange).not.toHaveBeenCalled();
    expect(onGestureEnd).toHaveBeenCalledWith(20);
  });

  it("a swipe that starts on the thumb puts back anything the grab moved", () => {
    const { slider, onChange, onGestureEnd } = setup();
    touch("Down", slider, 1, 20, THUMB_CENTRE_AT_20);
    touch("Move", slider, 1, 22, THUMB_CENTRE_AT_20 - 4); // the grab tracks at once: 22 %
    expect(onChange).toHaveBeenLastCalledWith(22);
    touch("Move", slider, 1, 40, THUMB_CENTRE_AT_20 - 5); // then 20 px sideways: a swipe
    expect(onChange).toHaveBeenLastCalledWith(20);
    expect(slider).toHaveAttribute("aria-valuenow", "200");
    touch("Move", slider, 1, 80, THUMB_CENTRE_AT_20 - 60); // ignored from here on
    touch("Cancel", slider, 1, 0, 0);
    expect(onChange).toHaveBeenLastCalledWith(20);
    expect(onChange).not.toHaveBeenCalledWith(100);
    expect(onGestureEnd).toHaveBeenCalledWith(20);
  });

  it("a hirer's ceiling still caps a tap and a touch drag", () => {
    const { slider, onChange } = setup({ ceiling: 60 });
    touch("Down", slider, 1, 20, 10);
    touch("Up", slider, 1, 20, 10); // a tap near the top of travel
    expect(onChange).toHaveBeenLastCalledWith(60);
    touch("Down", slider, 2, 20, 120);
    touch("Move", slider, 2, 20, 0);
    touch("Up", slider, 2, 20, 0);
    expect(onChange).toHaveBeenLastCalledWith(60);
    expect(onChange).not.toHaveBeenCalledWith(100);
  });

  it("a read-only fader ignores touch altogether", () => {
    const { slider, onChange, onGestureStart } = setup({ readOnly: true });
    touch("Down", slider, 1, 20, 100);
    touch("Up", slider, 1, 20, 100);
    expect(onChange).not.toHaveBeenCalled();
    expect(onGestureStart).not.toHaveBeenCalled();
  });

  it("two fingers drag two faders at once", () => {
    const first = vi.fn();
    const second = vi.fn();
    render(
      <>
        <FaderStrip label="A" value={20} scale={scale} onChange={first} />
        <FaderStrip label="B" value={20} scale={scale} onChange={second} />
      </>,
    );
    const a = screen.getByRole("slider", { name: "A fader" });
    const b = screen.getByRole("slider", { name: "B fader" });
    touch("Down", a, 1, 20, 100);
    touch("Down", b, 2, 20, 100);
    touch("Move", a, 1, 20, 80);
    touch("Move", b, 2, 20, 130); // down: B's own slop decision
    expect(first).toHaveBeenLastCalledWith(60);
    expect(second).toHaveBeenLastCalledWith(35);
    touch("Up", a, 1, 20, 60);
    touch("Up", b, 2, 20, 140);
    expect(first).toHaveBeenLastCalledWith(70);
    expect(second).toHaveBeenLastCalledWith(30);
  });

  it("mouse and pen still jump on press, as before", () => {
    const { slider, onChange } = setup();
    fireEvent.pointerDown(slider, { pointerId: 1, pointerType: "mouse", clientX: 20, clientY: 100, button: 0 });
    expect(onChange).toHaveBeenLastCalledWith(50);
    fireEvent.pointerMove(slider, { pointerId: 1, pointerType: "mouse", clientX: 60, clientY: 100 }); // sideways is still the fader's
    fireEvent.pointerUp(slider, { pointerId: 1, pointerType: "mouse", clientX: 60, clientY: 100 });
    expect(onChange).toHaveBeenLastCalledWith(50);
    fireEvent.pointerDown(slider, { pointerId: 2, pointerType: "pen", clientX: 20, clientY: 40, button: 0 });
    expect(onChange).toHaveBeenLastCalledWith(80);
    fireEvent.pointerUp(slider, { pointerId: 2, pointerType: "pen", clientX: 20, clientY: 40 });
  });

  it("the horizontal form waits the same way, with the axes swapped", () => {
    // The travel is the 44-wide RECT; the thumb sits at 20 % of it (x ≈ 9).
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      if (this.classList.contains("fader-thumb")) return { ...RECT, left: 3, right: 15, width: 12, x: 3 } as DOMRect;
      return RECT as DOMRect;
    });
    const onChange = vi.fn();
    render(<FaderStrip label="Master" value={20} scale={scale} onChange={onChange} orientation="horizontal" />);
    const slider = screen.getByRole("slider");
    // A vertical swipe from the track is the page's scroll ...
    touch("Down", slider, 1, 30, 20);
    touch("Move", slider, 1, 31, 40);
    touch("Cancel", slider, 1, 0, 0);
    expect(onChange).not.toHaveBeenCalled();
    // ... a drag along it is the fader's.
    touch("Down", slider, 2, 30, 20);
    touch("Move", slider, 2, 40, 21);
    touch("Up", slider, 2, 44, 21);
    expect(onChange).toHaveBeenLastCalledWith(100);
  });
});
