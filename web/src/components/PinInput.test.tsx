/* PIN boxes (spec §21.8): auto-advance, backspace steps back, paste fills all six, completion fires. */
import { fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import { PinInput } from "./PinInput";

function Harness({ onComplete }: { onComplete: (pin: string) => void }) {
  const [value, setValue] = useState("");
  return <PinInput value={value} onChange={setValue} onComplete={onComplete} />;
}

const boxes = () => screen.getAllByRole("textbox") as HTMLInputElement[];

describe("PinInput", () => {
  it("renders six numeric boxes", () => {
    render(<Harness onComplete={vi.fn()} />);
    const inputs = boxes();
    expect(inputs).toHaveLength(6);
    inputs.forEach((input) => expect(input).toHaveAttribute("inputmode", "numeric"));
  });

  it("auto-advances focus as digits are typed", () => {
    render(<Harness onComplete={vi.fn()} />);
    const inputs = boxes();
    inputs[0]!.focus();
    fireEvent.change(inputs[0]!, { target: { value: "4" } });
    expect(inputs[0]).toHaveValue("4");
    expect(document.activeElement).toBe(inputs[1]);
    fireEvent.change(inputs[1]!, { target: { value: "2" } });
    expect(inputs[1]).toHaveValue("2");
    expect(document.activeElement).toBe(inputs[2]);
  });

  it("ignores non-digits", () => {
    render(<Harness onComplete={vi.fn()} />);
    const inputs = boxes();
    fireEvent.change(inputs[0]!, { target: { value: "x" } });
    expect(inputs[0]).toHaveValue("");
  });

  it("steps back on backspace and clears the previous digit", () => {
    render(<Harness onComplete={vi.fn()} />);
    const inputs = boxes();
    fireEvent.change(inputs[0]!, { target: { value: "1" } });
    fireEvent.change(inputs[1]!, { target: { value: "2" } });
    expect(document.activeElement).toBe(inputs[2]);
    fireEvent.keyDown(inputs[2]!, { key: "Backspace" });
    expect(document.activeElement).toBe(inputs[1]);
    expect(inputs[1]).toHaveValue("");
    expect(inputs[0]).toHaveValue("1");
  });

  it("fills all six from a paste and fires completion once", () => {
    const onComplete = vi.fn();
    render(<Harness onComplete={onComplete} />);
    const inputs = boxes();
    fireEvent.paste(inputs[0]!, { clipboardData: { getData: () => "12 34-56" } });
    expect(inputs.map((i) => i.value).join("")).toBe("123456");
    expect(onComplete).toHaveBeenCalledTimes(1);
    expect(onComplete).toHaveBeenCalledWith("123456");
  });

  it("fires completion when the sixth digit is typed", () => {
    const onComplete = vi.fn();
    render(<Harness onComplete={onComplete} />);
    const inputs = boxes();
    "98765".split("").forEach((d, i) => fireEvent.change(inputs[i]!, { target: { value: d } }));
    expect(onComplete).not.toHaveBeenCalled();
    fireEvent.change(inputs[5]!, { target: { value: "4" } });
    expect(onComplete).toHaveBeenCalledWith("987654");
  });
});
