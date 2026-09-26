/*
 * Per-key subscription, at the level a component sees it (spec §21.2; §22.3
 * "a single-channel frame re-renders one component, not the view").
 *
 * The naive store keeps one version counter and notifies every subscriber on
 * every change: at 15 fps with fifty subscribed components, a two-channel
 * update re-renders the entire lighting view fifteen times a second. These
 * tests are what stops someone reintroducing that.
 */
import { render, screen, act } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";

import { applyMessage, resetLiveState, useColour, useDisplayLevel, useLevel, useMeter, useMixer } from "./store";

const renders: Record<string, number> = {};

function count(name: string): void {
  renders[name] = (renders[name] ?? 0) + 1;
}

function FixtureNode({ id }: { id: number }) {
  const level = useLevel(id);
  count(`fixture-${id}`);
  return <span data-testid={`fixture-${id}`}>{level ?? "—"}</span>;
}

function ColourSwatch({ id }: { id: number }) {
  const colour = useColour(id);
  count(`colour-${id}`);
  return <span data-testid={`colour-${id}`}>{colour ? `${colour.r},${colour.g},${colour.b}` : "—"}</span>;
}

function MeterBar({ id }: { id: number }) {
  const meter = useMeter(id);
  count(`meter-${id}`);
  // Absent, not at the floor: no data means no bar at all (§21.13, B58).
  return <span data-testid={`meter-${id}`}>{meter === null ? "none" : meter.join("/")}</span>;
}

function FaderStrip({ id }: { id: number }) {
  const strip = useMixer(id);
  count(`fader-${id}`);
  return (
    <span data-testid={`fader-${id}`}>
      {strip?.db ?? "—"}
      <MeterBar id={id} />
    </span>
  );
}

/** StagePlan subscribes to nothing; its children subscribe to their own channel. */
function StagePlan({ ids }: { ids: number[] }) {
  count("view");
  return (
    <div>
      {ids.map((id) => (
        <FixtureNode key={id} id={id} />
      ))}
      {ids.map((id) => (
        <ColourSwatch key={id} id={id} />
      ))}
    </div>
  );
}

function MixerView({ ids }: { ids: number[] }) {
  count("mixer-view");
  return (
    <div>
      {ids.map((id) => (
        <FaderStrip key={id} id={id} />
      ))}
    </div>
  );
}

beforeEach(() => {
  resetLiveState();
  for (const key of Object.keys(renders)) delete renders[key];
});

describe("per-key subscription", () => {
  it("re-renders one fixture and not the view", () => {
    render(<StagePlan ids={[1, 2, 3]} />);
    expect(renders["view"]).toBe(1);

    act(() => {
      applyMessage({ type: "lighting_state", channels: { "2": { level: 78.5 } }, source: "fade" });
    });

    expect(screen.getByTestId("fixture-2")).toHaveTextContent("78.5");
    expect(renders["fixture-2"]).toBe(2);
    expect(renders["fixture-1"]).toBe(1);
    expect(renders["fixture-3"]).toBe(1);
    expect(renders["view"]).toBe(1);
    // A level change is not a colour change.
    expect(renders["colour-2"]).toBe(1);
  });

  it("re-renders a strip's meter without re-rendering its fader", () => {
    render(<MixerView ids={[1, 2]} />);
    act(() => {
      applyMessage({ type: "mixer_state", inputs: { "1": { db: -5.0, muted: false, origin: "app" } } });
    });
    expect(renders["fader-1"]).toBe(2);
    const afterState = renders["meter-1"] as number;

    act(() => {
      applyMessage({ type: "mixer_meters", channels: { "1": [-12.4] }, at: 1757462011.482 });
    });

    expect(screen.getByTestId("meter-1")).toHaveTextContent("-12.4");
    expect(renders["meter-1"]).toBe(afterState + 1);
    expect(renders["fader-1"]).toBe(2); // the fader did not move
    expect(renders["fader-2"]).toBe(1);
    expect(renders["mixer-view"]).toBe(1);
    expect(screen.getByTestId("meter-2")).toHaveTextContent("none");
  });

  it("does not re-render at all when a frame repeats a value", () => {
    render(<StagePlan ids={[1]} />);
    act(() => {
      applyMessage({ type: "lighting_state", channels: { "1": { level: 50 } } });
    });
    expect(renders["fixture-1"]).toBe(2);
    act(() => {
      applyMessage({ type: "lighting_state", channels: { "1": { level: 50 } } });
    });
    expect(renders["fixture-1"]).toBe(2);
  });

  it("follows the external-control display rule without merging the two maps", () => {
    function Observed({ id }: { id: number }) {
      const level = useDisplayLevel(id);
      count(`observed-${id}`);
      return <span data-testid={`observed-${id}`}>{level ?? "—"}</span>;
    }
    render(<Observed id={1} />);
    act(() => {
      applyMessage({ type: "lighting_state", channels: { "1": { level: 40 } } });
    });
    expect(screen.getByTestId("observed-1")).toHaveTextContent("40");
    act(() => {
      applyMessage({ type: "lighting_state", external_control: "detected", observed: { "1": 85.5 } });
    });
    expect(screen.getByTestId("observed-1")).toHaveTextContent("85.5");
  });
});
