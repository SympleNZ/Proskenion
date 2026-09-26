/*
 * The stage plan (spec §9.3, §21.5, §21.12, §7.2.7). Covers the acceptance
 * list from the task brief directly: orientation, the capability table,
 * dragging and its 409, Set level's single request at the shared fade time,
 * a patch conflict's icon-and-word, external control, and the roving
 * keyboard pattern with its once-only fade announcement.
 */
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Device } from "@/admin/devices/types";
import { ApiError } from "@/api/client";
import { fadeMs, resetFadeSeconds, setFadeSeconds } from "@/lighting/fadeTime";
import type { FixtureProfile, LightingChannel } from "@/lighting/types";
import { resetLiveState, setExternalControl, setLevel, setObserved } from "@/live/store";

import { StagePlan } from "./StagePlan";
import { barY, fixtureX, viewHeight, SIDE_MARGIN, VIEW_WIDTH } from "./layout";
import type { LightingBar } from "./types";

const PROFILES: FixtureProfile[] = [
  {
    id: 1,
    manufacturer: null,
    model: null,
    name: "Single-channel dimmer",
    channel_count: 1,
    channels: [{ offset: 0, role: "dimmer", default: 0 }],
    updated_at: "2026-01-01T00:00:00+13:00",
  },
];

const DEVICES: Device[] = [
  {
    id: 1,
    category: "lighting",
    driver_key: "dmx_test",
    name: "DMX Universe 1",
    enabled: true,
    config: {},
    created_at: "2026-01-01T00:00:00+13:00",
    updated_at: "2026-01-01T00:00:00+13:00",
  },
];

const client = vi.hoisted(() => ({ api: vi.fn() }));

vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: client.api,
}));

vi.mock("@/live/socket", () => ({ send: vi.fn() }));

const BARS: LightingBar[] = [
  { id: 1, name: "Proscenium", sort_order: 0, notes: null, updated_at: "2026-01-01T00:00:00+13:00" },
  { id: 2, name: "Bar 2", sort_order: 1, notes: null, updated_at: "2026-01-01T00:00:00+13:00" },
];

function channel(overrides: Partial<LightingChannel> = {}): LightingChannel {
  return {
    id: 1,
    name: "Stage Wash 1",
    type: "dmx",
    min_value: 0,
    max_value: 100,
    has_colour: false,
    group_ids: [],
    bar_id: 1,
    position: 0,
    visible_staff: true,
    updated_at: "2026-01-01T00:00:00+13:00",
    ...overrides,
  };
}

const FIXTURES: LightingChannel[] = [
  channel({ id: 1, name: "Stage Wash 1", bar_id: 1, position: 0.0 }),
  channel({ id: 2, name: "Stage Wash 2", bar_id: 1, position: 1.0 }),
  channel({ id: 3, name: "Upstage Wash", type: "knx_dimmer", bar_id: 2, position: 0.5 }),
];

function renderPlan(props: Partial<React.ComponentProps<typeof StagePlan>> = {}, wrapper?: (ui: ReactNode) => ReactNode) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  const ui = (
    <StagePlan
      mode="operator"
      bars={BARS}
      fixtures={FIXTURES}
      conflicts={new Set<number>()}
      profiles={PROFILES}
      devices={DEVICES}
      {...props}
    />
  );
  return render(<QueryClientProvider client={queryClient}>{wrapper ? wrapper(ui) : ui}</QueryClientProvider>);
}

beforeEach(() => {
  resetLiveState();
  resetFadeSeconds();
  client.api.mockReset();
  client.api.mockResolvedValue(undefined);
});

afterEach(() => {
  vi.useRealTimers();
});

describe("orientation (§9.3, §21.12): bar 0 renders lowest, position 0.0 renders left of 1.0", () => {
  it("places the proscenium (bar 1, sort_order 0) below Bar 2 (sort_order 1), and position 0 left of position 1", () => {
    renderPlan({ mode: "admin" });
    function xy(id: number): { x: number; y: number } {
      const transform = screen.getByTestId(`fixture-node-${id}`).getAttribute("transform") ?? "";
      const match = /translate\(([-\d.]+) ([-\d.]+)\)/.exec(transform);
      return { x: Number(match?.[1]), y: Number(match?.[2]) };
    }
    const wash1 = xy(1); // bar 1 (proscenium), position 0.0
    const wash2 = xy(2); // bar 1 (proscenium), position 1.0
    const upstage = xy(3); // bar 2, position 0.5

    expect(wash1.y).toBeGreaterThan(upstage.y); // the proscenium sits lower on the plan
    expect(wash1.y).toBe(wash2.y); // same bar, same row
    expect(wash1.x).toBeLessThan(wash2.x); // position 0.0 renders to the left of 1.0
  });
});

describe("the capability table (§21.12): operator", () => {
  it("can tap to select", () => {
    renderPlan({ mode: "operator" });
    const node = screen.getByTestId("fixture-node-1");
    fireEvent.pointerDown(node, { pointerId: 1, clientX: 10, clientY: 10 });
    fireEvent.pointerUp(node, { pointerId: 1, clientX: 10, clientY: 10 });
    expect(node).toHaveAttribute("aria-pressed", "true");
  });

  it("can double-tap to open the fader", async () => {
    renderPlan({ mode: "operator" });
    const node = screen.getByTestId("fixture-node-1");
    fireEvent.pointerDown(node, { pointerId: 1, clientX: 10, clientY: 10 });
    fireEvent.pointerUp(node, { pointerId: 1, clientX: 10, clientY: 10 });
    fireEvent.pointerDown(node, { pointerId: 1, clientX: 10, clientY: 10 });
    fireEvent.pointerUp(node, { pointerId: 1, clientX: 10, clientY: 10 });
    expect(await screen.findByRole("slider", { name: "Stage Wash 1 fader" })).toBeInTheDocument();
  });

  it("never sees multi-select, the admin toolbar or the selection bar", () => {
    renderPlan({ mode: "operator" });
    const one = screen.getByTestId("fixture-node-1");
    const two = screen.getByTestId("fixture-node-2");
    for (const node of [one, two]) {
      fireEvent.pointerDown(node, { pointerId: 1, clientX: 10, clientY: 10 });
      fireEvent.pointerUp(node, { pointerId: 1, clientX: 10, clientY: 10 });
    }
    // Tapping a second fixture replaced the selection rather than adding to it.
    expect(one).toHaveAttribute("aria-pressed", "false");
    expect(two).toHaveAttribute("aria-pressed", "true");
    expect(screen.queryByRole("toolbar", { name: "Selected fixtures" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add fixture" })).not.toBeInTheDocument();
  });

  it("cannot drag a fixture", () => {
    renderPlan({ mode: "operator" });
    const node = screen.getByTestId("fixture-node-1");
    fireEvent.pointerDown(node, { pointerId: 1, clientX: 10, clientY: 10 });
    fireEvent.pointerMove(node, { pointerId: 1, clientX: 200, clientY: 200 });
    fireEvent.pointerUp(node, { pointerId: 1, clientX: 200, clientY: 200 });
    expect(client.api).not.toHaveBeenCalledWith(expect.stringContaining("/lighting/channels/1"), expect.anything());
  });
});

describe("the capability table (§21.12): admin", () => {
  it("can multi-select and sees the selection bar with Group and Set level", () => {
    renderPlan({ mode: "admin" });
    const one = screen.getByTestId("fixture-node-1");
    const two = screen.getByTestId("fixture-node-2");
    for (const node of [one, two]) {
      fireEvent.pointerDown(node, { pointerId: 1, clientX: 10, clientY: 10 });
      fireEvent.pointerUp(node, { pointerId: 1, clientX: 10, clientY: 10 });
    }
    expect(one).toHaveAttribute("aria-pressed", "true");
    expect(two).toHaveAttribute("aria-pressed", "true");
    const bar = screen.getByRole("toolbar", { name: "Selected fixtures" });
    expect(within(bar).getByText("2 selected")).toBeInTheDocument();
    expect(within(bar).getByRole("button", { name: "Group" })).toBeInTheDocument();
    expect(within(bar).getByRole("button", { name: "Set level" })).toBeInTheDocument();
  });

  it("sees the reorder-bars and add-fixture admin controls", () => {
    renderPlan({ mode: "admin" });
    expect(screen.getByRole("button", { name: "Add fixture" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Move Bar 2 upstage/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Move Bar 2 downstage/ })).toBeInTheDocument();
  });

  it("the Group action names the selection and creates it in one request", async () => {
    renderPlan({ mode: "admin" });
    const one = screen.getByTestId("fixture-node-1");
    const two = screen.getByTestId("fixture-node-2");
    for (const node of [one, two]) {
      fireEvent.pointerDown(node, { pointerId: 1, clientX: 10, clientY: 10 });
      fireEvent.pointerUp(node, { pointerId: 1, clientX: 10, clientY: 10 });
    }
    fireEvent.click(screen.getByRole("button", { name: "Group" }));
    fireEvent.change(await screen.findByLabelText("Group name"), { target: { value: "Wash row" } });
    fireEvent.click(screen.getByRole("button", { name: "Create group" }));

    await waitFor(() => {
      expect(client.api).toHaveBeenCalledWith("/lighting/groups", { body: { name: "Wash row", channel_ids: [1, 2] } });
    });
    // The selection clears once the group is created.
    await waitFor(() => expect(screen.queryByRole("toolbar", { name: "Selected fixtures" })).not.toBeInTheDocument());
  });

  it("long-pressing a fixture opens its context menu, and Remove from bar unassigns it", async () => {
    vi.useFakeTimers();
    renderPlan({ mode: "admin" });
    const node = screen.getByTestId("fixture-node-1");
    fireEvent.pointerDown(node, { pointerId: 1, clientX: 15, clientY: 25 });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(600);
    });
    const menu = screen.getByRole("menu", { name: "Fixture actions" });
    expect(within(menu).getByRole("menuitem", { name: "Open fader" })).toBeInTheDocument();

    fireEvent.click(within(menu).getByRole("menuitem", { name: "Remove from bar" }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(client.api).toHaveBeenCalledWith(
      "/lighting/channels/1",
      expect.objectContaining({ method: "PUT", body: { bar_id: null, position: null } }),
    );
  });

  it("Add fixture sends the new fixture's fields", async () => {
    renderPlan({ mode: "admin" });
    fireEvent.click(screen.getByRole("button", { name: "Add fixture" }));
    const dialog = await screen.findByRole("dialog", { name: "Add fixture" });
    fireEvent.change(within(dialog).getByLabelText("Name"), { target: { value: "New Wash" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Add fixture" }));

    await waitFor(() => {
      expect(client.api).toHaveBeenCalledWith(
        "/lighting/channels",
        expect.objectContaining({ body: expect.objectContaining({ name: "New Wash", bar_id: 1 }) }),
      );
    });
  });
});

describe("dragging a fixture (§21.12, admin only)", () => {
  beforeEach(() => {
    vi.spyOn(SVGElement.prototype, "getBoundingClientRect").mockReturnValue({
      left: 0,
      top: 0,
      width: VIEW_WIDTH,
      height: viewHeight(2),
      right: VIEW_WIDTH,
      bottom: viewHeight(2),
      x: 0,
      y: 0,
      toJSON: () => ({}),
    } as DOMRect);
  });

  it("sends the fixture's new bar_id and position", async () => {
    renderPlan({ mode: "admin" });
    const node = screen.getByTestId("fixture-node-1"); // starts at bar 1 (y=barY(0,2)), position 0.0 (x=SIDE_MARGIN)
    const startX = fixtureX(0);
    const startY = barY(0, 2);
    // Land squarely on bar 2's row (index 1), halfway across.
    const dropY = barY(1, 2);
    const dropX = SIDE_MARGIN + (VIEW_WIDTH - SIDE_MARGIN * 2) * 0.5;

    fireEvent.pointerDown(node, { pointerId: 1, clientX: startX, clientY: startY });
    fireEvent.pointerMove(node, { pointerId: 1, clientX: dropX, clientY: dropY });
    fireEvent.pointerUp(node, { pointerId: 1, clientX: dropX, clientY: dropY });

    await waitFor(() => {
      expect(client.api).toHaveBeenCalledWith(
        "/lighting/channels/1",
        expect.objectContaining({
          method: "PUT",
          body: { bar_id: 2, position: 0.5 },
          headers: { "If-Unmodified-Since-Version": "2026-01-01T00:00:00+13:00" },
        }),
      );
    });
  });

  it("a 409 conflict offers reload or overwrite", async () => {
    const current = channel({ id: 1, bar_id: 2, position: 0.9, updated_at: "2026-01-01T01:00:00+13:00" });
    client.api.mockImplementation((path: string, options?: { method?: string }) => {
      if (path === "/lighting/channels/1" && options?.method === "PUT") {
        return Promise.reject(new ApiError(409, "conflict", "This fixture was moved by someone else", { current }));
      }
      return Promise.resolve(undefined);
    });

    renderPlan({ mode: "admin" });
    const node = screen.getByTestId("fixture-node-1");
    const startX = fixtureX(0);
    const startY = barY(0, 2);
    const dropX = SIDE_MARGIN + (VIEW_WIDTH - SIDE_MARGIN * 2) * 0.5;
    const dropY = barY(1, 2);

    fireEvent.pointerDown(node, { pointerId: 1, clientX: startX, clientY: startY });
    fireEvent.pointerMove(node, { pointerId: 1, clientX: dropX, clientY: dropY });
    fireEvent.pointerUp(node, { pointerId: 1, clientX: dropX, clientY: dropY });

    const dialog = await screen.findByRole("alertdialog");
    expect(within(dialog).getByRole("button", { name: "Reload theirs" })).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Overwrite with mine" })).toBeInTheDocument();

    fireEvent.click(within(dialog).getByRole("button", { name: "Overwrite with mine" }));
    await waitFor(() => {
      expect(client.api).toHaveBeenCalledWith(
        "/lighting/channels/1",
        expect.objectContaining({
          method: "PUT",
          body: { bar_id: 2, position: 0.5 },
          headers: { "If-Unmodified-Since-Version": "2026-01-01T01:00:00+13:00" },
        }),
      );
    });
  });
});

describe("Set level (§21.12, admin only)", () => {
  it("sends one POST /lighting/levels with the shared fade time, applied proportionally", async () => {
    setLevel(1, 80);
    setLevel(2, 40);
    setFadeSeconds(3);

    renderPlan({ mode: "admin" });
    const one = screen.getByTestId("fixture-node-1");
    const two = screen.getByTestId("fixture-node-2");
    for (const node of [one, two]) {
      fireEvent.pointerDown(node, { pointerId: 1, clientX: 10, clientY: 10 });
      fireEvent.pointerUp(node, { pointerId: 1, clientX: 10, clientY: 10 });
    }

    fireEvent.click(screen.getByRole("button", { name: "Set level" }));
    const slider = await screen.findByLabelText("Level");
    fireEvent.change(slider, { target: { value: "100" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));

    await waitFor(() => expect(client.api).toHaveBeenCalledTimes(1));
    expect(client.api).toHaveBeenCalledWith("/lighting/levels", {
      body: { levels: { "1": 100, "2": 50 }, fade_ms: 3000 },
    });
  });

  it("the Lighting view's Fade control and the stage plan's Set level read the same shared value", () => {
    expect(fadeMs()).toBe(2000); // the default, before either view has touched it
    setFadeSeconds(4.5);
    expect(fadeMs()).toBe(4500); // both views would now see 4.5s
  });
});

describe("patch conflicts (§9.1, §21.12): marked with an icon and a word, never colour alone", () => {
  it("shows the conflict marker on the conflicting fixture only", () => {
    renderPlan({ mode: "operator", conflicts: new Set([1]) });
    expect(screen.getByTestId("fixture-node-1")).toHaveAttribute("aria-label", expect.stringContaining("patch conflict"));
    expect(screen.getByTestId("fixture-node-2")).not.toHaveAttribute("aria-label", expect.stringContaining("patch conflict"));
    expect(screen.getByText(/Conflict/)).toBeInTheDocument();
  });
});

describe("external control (§7.2.7, §21.11)", () => {
  it("a DMX fixture is read-only, showing the observed level, while a KNX dimmer stays live", () => {
    act(() => {
      setExternalControl("detected");
      setObserved(1, 92.3);
    });
    renderPlan({ mode: "operator" });
    expect(screen.getByTestId("fixture-node-1")).toHaveAttribute("aria-readonly", "true"); // dmx
    expect(screen.getByTestId("fixture-node-3")).not.toHaveAttribute("aria-readonly"); // knx_dimmer
  });

  function select(...ids: number[]): void {
    for (const id of ids) {
      const node = screen.getByTestId(`fixture-node-${id}`);
      fireEvent.pointerDown(node, { pointerId: 1, clientX: 10, clientY: 10 });
      fireEvent.pointerUp(node, { pointerId: 1, clientX: 10, clientY: 10 });
    }
  }

  it.each(["detected", "manual"] as const)(
    "Set level on a selection of stage fixtures is unavailable (%s), and the bar says why",
    (state) => {
      act(() => setExternalControl(state));
      renderPlan({ mode: "admin" });
      select(1, 2);
      const bar = screen.getByRole("toolbar", { name: "Selected fixtures" });
      const button = within(bar).getByRole("button", { name: "Set level" });
      expect(button).toBeDisabled();
      expect(button).toHaveAccessibleDescription("Stage fixtures are read-only under external control");
      expect(within(bar).getByRole("button", { name: "Group" })).toBeEnabled(); // grouping is not a level
      fireEvent.click(button);
      expect(screen.queryByLabelText("Level")).not.toBeInTheDocument();
      expect(client.api).not.toHaveBeenCalled();
    },
  );

  it("Set level on a selection of house dimmers only stays live under external control", async () => {
    act(() => setExternalControl("detected"));
    setLevel(3, 40);
    setFadeSeconds(1);
    renderPlan({ mode: "admin" });
    select(3);
    const button = within(screen.getByRole("toolbar", { name: "Selected fixtures" })).getByRole("button", {
      name: "Set level",
    });
    expect(button).toBeEnabled();
    fireEvent.click(button);
    fireEvent.change(await screen.findByLabelText("Level"), { target: { value: "70" } });
    expect(screen.queryByText(/skipped/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));

    await waitFor(() => expect(client.api).toHaveBeenCalledTimes(1));
    expect(client.api).toHaveBeenCalledWith("/lighting/levels", { body: { levels: { "3": 70 }, fade_ms: 1000 } });
  });

  it("Set level on a mixed selection sets only the house dimmers, and says the stage fixtures were skipped", async () => {
    act(() => {
      setExternalControl("detected");
      setObserved(1, 90); // the desk's level: not the controller's to scale against
    });
    setLevel(1, 10);
    setLevel(2, 20);
    setLevel(3, 50);
    setFadeSeconds(2);
    renderPlan({ mode: "admin" });
    select(1, 2, 3);
    fireEvent.click(within(screen.getByRole("toolbar", { name: "Selected fixtures" })).getByRole("button", { name: "Set level" }));

    const sheet = await screen.findByRole("dialog");
    expect(within(sheet).getByText("Set level — 3 selected")).toBeInTheDocument();
    expect(within(sheet).getByRole("status")).toHaveTextContent(
      "External control is active: 2 stage fixtures skipped. Only the 1 house dimmer is set.",
    );
    const slider = within(sheet).getByLabelText("Level");
    expect(slider).toHaveValue("50"); // starts from the house dimmer, not the desk's 90
    fireEvent.change(slider, { target: { value: "80" } });
    fireEvent.click(within(sheet).getByRole("button", { name: "Apply" }));

    await waitFor(() => expect(client.api).toHaveBeenCalledTimes(1));
    expect(client.api).toHaveBeenCalledWith("/lighting/levels", { body: { levels: { "3": 80 }, fade_ms: 2000 } });
  });

  it("a desk connecting while Set level is open on stage fixtures disables Apply, and the sheet says why", async () => {
    renderPlan({ mode: "admin" });
    select(1, 2);
    fireEvent.click(within(screen.getByRole("toolbar", { name: "Selected fixtures" })).getByRole("button", { name: "Set level" }));
    const sheet = await screen.findByRole("dialog");
    expect(within(sheet).getByRole("button", { name: "Apply" })).toBeEnabled();

    act(() => setExternalControl("detected"));

    expect(within(sheet).getByRole("status")).toHaveTextContent(
      "External control is active: stage fixtures are read-only, and nothing selected can be set.",
    );
    const apply = within(sheet).getByRole("button", { name: "Apply" });
    expect(apply).toBeDisabled();
    fireEvent.click(apply);
    expect(client.api).not.toHaveBeenCalled();
  });

  it("with external control off, a mixed selection's Set level sets every fixture and skips nothing", async () => {
    setLevel(1, 50);
    setLevel(3, 100);
    renderPlan({ mode: "admin" });
    select(1, 3);
    fireEvent.click(within(screen.getByRole("toolbar", { name: "Selected fixtures" })).getByRole("button", { name: "Set level" }));
    const sheet = await screen.findByRole("dialog");
    expect(within(sheet).queryByRole("status")).not.toBeInTheDocument();
    fireEvent.change(within(sheet).getByLabelText("Level"), { target: { value: "50" } });
    fireEvent.click(within(sheet).getByRole("button", { name: "Apply" }));
    await waitFor(() => expect(client.api).toHaveBeenCalledTimes(1));
    expect(client.api).toHaveBeenCalledWith("/lighting/levels", { body: { levels: { "1": 25, "3": 50 }, fade_ms: 2000 } });
  });
});

describe("the roving tab stop (§21.12, §24.2)", () => {
  it("gives exactly one fixture tabIndex 0, moves it with the arrow keys, and never gives Tab sixteen stops", () => {
    renderPlan({ mode: "operator" });
    const one = screen.getByTestId("fixture-node-1");
    const two = screen.getByTestId("fixture-node-2");
    const three = screen.getByTestId("fixture-node-3");
    expect(one).toHaveAttribute("tabindex", "0");
    expect(two).toHaveAttribute("tabindex", "-1");
    expect(three).toHaveAttribute("tabindex", "-1");

    one.focus();
    fireEvent.keyDown(one, { key: "ArrowRight" });

    expect(screen.getByTestId("fixture-node-1")).toHaveAttribute("tabindex", "-1");
    expect(screen.getByTestId("fixture-node-2")).toHaveAttribute("tabindex", "0");
  });

  it("announces a fade's completion once, after it finishes — not during", async () => {
    vi.useFakeTimers();
    setLevel(1, 50);
    setFadeSeconds(2);
    renderPlan({ mode: "admin" });

    const node = screen.getByTestId("fixture-node-1");
    fireEvent.pointerDown(node, { pointerId: 1, clientX: 10, clientY: 10 });
    fireEvent.pointerUp(node, { pointerId: 1, clientX: 10, clientY: 10 });
    fireEvent.click(screen.getByRole("button", { name: "Set level" }));
    fireEvent.change(screen.getByLabelText("Level"), { target: { value: "100" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));

    // Fake timers replace the global timer testing-library's async queries
    // would otherwise poll with, so the mutation's own microtasks are
    // flushed explicitly instead of via `waitFor`/`findBy*`.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(client.api).toHaveBeenCalledTimes(1);

    const live = document.querySelector('[aria-live="polite"]');
    expect(live?.textContent).toBe(""); // not during

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    expect(live?.textContent).toBe("Stage Wash 1 faded to 100 percent.");

    // Advancing further does not repeat or change the announcement.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    expect(live?.textContent).toBe("Stage Wash 1 faded to 100 percent.");
  });
});
