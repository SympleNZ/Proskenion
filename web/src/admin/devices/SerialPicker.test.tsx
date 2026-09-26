/*
 * Serial device picker (spec §21.24 *Serial device picker*, §5.5, §24.1).
 *
 * The two flags have to be readable as words, not inferred from a colour, and
 * Refresh has to re-enumerate: hot-plugging during commissioning is normal.
 */
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { renderWithProviders } from "@/test/render";

import { PORTS } from "./fixtures";
import { SerialPicker, IDENTIFY_UNAVAILABLE } from "./SerialPicker";

const client = vi.hoisted(() => ({ api: vi.fn() }));

vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: client.api,
}));

function renderPicker(value = "") {
  const onChange = vi.fn();
  renderWithProviders(<SerialPicker id="picker" value={value} onChange={onChange} />, { route: "/admin/devices" });
  return { onChange };
}

describe("SerialPicker", () => {
  beforeEach(() => {
    client.api.mockReset();
    client.api.mockResolvedValue({ ports: PORTS });
  });

  it("lists each port with its metadata and stores the by-id path", async () => {
    const { onChange } = renderPicker();

    const option = await screen.findByLabelText(/FTDI USB-RS232 Cable/);
    expect(screen.getAllByText("0403:6001")).toHaveLength(2);
    expect(
      screen.getByText("/dev/serial/by-id/usb-FTDI_USB-RS232_Cable_FTB6SPL2-if00-port0", { selector: ".serial-path" }),
    ).toBeInTheDocument();

    fireEvent.click(option);
    expect(onChange).toHaveBeenCalledWith("/dev/serial/by-id/usb-FTDI_USB-RS232_Cable_FTB6SPL2-if00-port0");
  });

  it("says *in use* in words and names what holds the port", async () => {
    renderPicker();
    expect(await screen.findByText("in use — DMX output")).toBeInTheDocument();
  });

  it("says *no stable path* in words and explains that the cable will move", async () => {
    renderPicker();
    const flag = await screen.findByText(/no stable path/);
    expect(flag.textContent).toMatch(/no serial number/);
    expect(flag.textContent).toMatch(/path will move/);
  });

  it("re-enumerates on Refresh, because a list captured at page load is wrong within a minute", async () => {
    renderPicker();
    await screen.findByLabelText(/FTDI USB-RS232 Cable/);
    expect(client.api).toHaveBeenCalledTimes(1);
    expect(client.api).toHaveBeenCalledWith("/drivers/serial-ports");

    client.api.mockResolvedValue({ ports: [PORTS[0]] });
    fireEvent.click(screen.getByRole("button", { name: /Refresh/ }));

    await waitFor(() => expect(client.api).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.queryByText("in use — DMX output")).not.toBeInTheDocument());
  });

  it("renders Identify disabled and names the gap rather than wiring it to nothing", async () => {
    renderPicker();
    const identify = await screen.findByRole("button", { name: /Identify/ });
    expect(identify).toBeDisabled();
    expect(identify).toHaveAttribute("title", IDENTIFY_UNAVAILABLE);
    expect(screen.getByText(IDENTIFY_UNAVAILABLE)).toBeInTheDocument();
  });

  it("says plainly when a stored path is not present now", async () => {
    renderPicker("/dev/serial/by-id/usb-Gone-if00-port0");
    expect(await screen.findByText(/not present now/)).toBeInTheDocument();
  });

  it("offers a retry when enumeration fails, and says when nothing is plugged in", async () => {
    client.api.mockResolvedValue({ ports: [] });
    renderPicker();
    expect(await screen.findByText("No serial devices found")).toBeInTheDocument();
  });
});
