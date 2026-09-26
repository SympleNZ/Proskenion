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
