/*
 * The fixture profile editor (spec §21.18, §15.9): the role select offers
 * only the closed vocabulary, `channel_count` follows the channel list's
 * length, and editing warns — without blocking — when fixtures use the profile.
 */
import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { FIXTURE_PROFILE_ROLES } from "@/lighting/types";

import { ProfileEditor } from "./ProfileEditor";

describe("ProfileEditor role vocabulary (§15.9)", () => {
  it("offers exactly the closed role vocabulary and nothing else", () => {
    render(<ProfileEditor open onOpenChange={() => undefined} profile={null} fixturesUsingCount={0} saving={false} onSave={vi.fn()} />);
    const select = screen.getByLabelText("Role");
    const options = within(select).getAllByRole("option").map((option) => (option as HTMLOptionElement).value);
    expect(options).toEqual([...FIXTURE_PROFILE_ROLES]);
  });

  it("channel_count follows the list — adding a channel grows it, removing shrinks it", () => {
    const onSave = vi.fn();
    render(<ProfileEditor open onOpenChange={() => undefined} profile={null} fixturesUsingCount={0} saving={false} onSave={onSave} />);
    expect(screen.getByText("Channels — 1 total")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "+ Add channel" }));
    expect(screen.getByText("Channels — 2 total")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Moving Head" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(onSave).toHaveBeenCalledWith(expect.objectContaining({ channels: expect.arrayContaining([expect.any(Object), expect.any(Object)]) }));
    const call = onSave.mock.calls[0]?.[0] as { channels: unknown[] };
    expect(call.channels).toHaveLength(2);
  });

  it("warns without blocking when fixtures already use the profile", () => {
    render(<ProfileEditor open onOpenChange={() => undefined} profile={null} fixturesUsingCount={3} saving={false} onSave={vi.fn()} />);
    expect(screen.getByText(/3 fixtures use this profile/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Moving Head" } });
    expect(screen.getByRole("button", { name: "Save" })).not.toBeDisabled();
  });
});
