/*
 * Help coverage for Mixer configuration (spec §19.1, §21.21): the Channels
 * and Outputs sections' add sheets (including the "gang another reference"
 * picker and the hirer-ceiling control), Main, and the desk scene library
 * (including its Test recall control).
 */
import { fireEvent, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const client = vi.hoisted(() => ({ api: vi.fn() }));
vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: client.api,
}));

import { describeMissing, findMissingHelp } from "@/help/coverage";
import { renderWithProviders } from "@/test/render";

import { ChannelsSection } from "./ChannelsSection";
import { DeskScenesSection } from "./DeskScenesSection";
import { CQ20B_FADER_LAW, CQ20B_REFS, DESK_SCENES, INPUT_CHANNEL, MAIN_CHANNEL, MIXER_DEVICE_ID, OUTPUT_CHANNEL } from "./fixtures";
import { MainPanel } from "./MainPanel";
import { OutputsSection } from "./OutputsSection";

function assertCovered(): void {
  const missing = findMissingHelp(document.body);
  expect(missing, describeMissing(missing)).toEqual([]);
}

beforeEach(() => {
  client.api.mockReset();
  client.api.mockResolvedValue(undefined);
});

describe("Mixer configuration gives every field and primary action help (spec §19.1)", () => {
  it("Channels section, an existing channel's Delete, and the add-channel sheet with a ganged reference", () => {
    renderWithProviders(
      <ChannelsSection deviceId={MIXER_DEVICE_ID} channels={[INPUT_CHANNEL]} refs={CQ20B_REFS} faderLaw={CQ20B_FADER_LAW} />,
      { route: "/admin/mixer" },
    );
    // Proves the existing channel's row — Delete included — actually rendered.
    screen.getByRole("button", { name: "Delete" });
    fireEvent.click(screen.getByRole("button", { name: "+ Add channel" }));
    // Choosing a reference reveals "+ Gang another" and the hirer-ceiling slider.
    fireEvent.change(screen.getByRole("combobox", { name: "Maps to" }), { target: { value: CQ20B_REFS[0]?.ref } });
    fireEvent.click(screen.getByLabelText("No ceiling"));
    assertCovered();
  });

  it("Outputs section, including its add sheet", () => {
    renderWithProviders(
      <OutputsSection deviceId={MIXER_DEVICE_ID} outputs={[OUTPUT_CHANNEL]} refs={CQ20B_REFS} faderLaw={CQ20B_FADER_LAW} />,
      { route: "/admin/mixer" },
    );
    fireEvent.click(screen.getByRole("button", { name: "+ Add output" }));
    assertCovered();
  });

  it("Main output panel", () => {
    renderWithProviders(<MainPanel channel={MAIN_CHANNEL} faderLaw={CQ20B_FADER_LAW} />, { route: "/admin/mixer" });
    assertCovered();
  });

  it("Desk scene library, including the add sheet and an existing scene's Test recall", () => {
    renderWithProviders(<DeskScenesSection deviceId={MIXER_DEVICE_ID} scenes={DESK_SCENES} recallSupported />, {
      route: "/admin/mixer",
    });
    fireEvent.click(screen.getByRole("button", { name: "+ Add scene" }));
    assertCovered();

    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    fireEvent.click(screen.getAllByRole("button", { name: "Edit" })[0] as HTMLElement);
    assertCovered();
  });
});
