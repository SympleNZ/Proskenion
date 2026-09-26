/*
 * Hirer PIN (spec §21.8): six individual boxes. Tapping any box opens the
 * numeric keypad on mobile; digits auto-advance; backspace steps back; paste
 * fills all six. On completion the boxes glow teal left to right and the
 * request fires. On a wrong PIN the boxes shake briefly, then clear.
 */
import { useEffect, useRef, type ChangeEvent, type ClipboardEvent, type CSSProperties, type KeyboardEvent } from "react";

import { cn } from "@/lib/utils";

export const PIN_LENGTH = 6;

export interface PinInputProps {
  value: string;
  onChange(value: string): void;
  onComplete(pin: string): void;
  disabled?: boolean;
  /** Wrong PIN: shake, then the parent clears the value. */
  shaking?: boolean;
  /** Complete: glow left to right. */
  glowing?: boolean;
  invalid?: boolean;
  label?: string;
  describedBy?: string;
}

const digitsOnly = (text: string): string => text.replace(/\D+/g, "");

export function PinInput({
  value,
  onChange,
  onComplete,
  disabled = false,
  shaking = false,
  glowing = false,
  invalid = false,
  label = "Venue PIN",
  describedBy,
}: PinInputProps) {
  const refs = useRef<Array<HTMLInputElement | null>>([]);
  const firedFor = useRef<string | null>(null);

  const focusBox = (index: number) => {
    const target = refs.current[Math.max(0, Math.min(PIN_LENGTH - 1, index))];
    target?.focus();
    target?.select();
  };

  const commit = (next: string) => {
    const clean = digitsOnly(next).slice(0, PIN_LENGTH);
    onChange(clean);
    if (clean.length === PIN_LENGTH && firedFor.current !== clean) {
      firedFor.current = clean;
      onComplete(clean);
    }
    if (clean.length < PIN_LENGTH) firedFor.current = null;
  };

  // If the parent clears the value (wrong PIN), start again from the first box.
  useEffect(() => {
    if (value === "" && !disabled && document.activeElement && refs.current.includes(document.activeElement as HTMLInputElement)) {
      focusBox(0);
    }
  }, [value, disabled]);

  const handleChange = (index: number) => (event: ChangeEvent<HTMLInputElement>) => {
    const typed = digitsOnly(event.target.value);
    if (!typed) {
      // Deleted the digit in this box.
      commit(value.slice(0, index) + value.slice(index + 1));
      return;
    }
    // One digit, or several from an autofill: place from this box onwards.
    const next = (value.slice(0, index) + typed + value.slice(index + typed.length)).slice(0, PIN_LENGTH);
    commit(next);
    focusBox(Math.min(index + typed.length, PIN_LENGTH - 1));
  };

  const handleKeyDown = (index: number) => (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "Backspace") {
      event.preventDefault();
      if (value[index]) {
        commit(value.slice(0, index) + value.slice(index + 1));
        focusBox(index);
      } else if (index > 0) {
        commit(value.slice(0, index - 1) + value.slice(index));
        focusBox(index - 1);
      }
    } else if (event.key === "ArrowLeft") {
      event.preventDefault();
      focusBox(index - 1);
    } else if (event.key === "ArrowRight") {
      event.preventDefault();
      focusBox(index + 1);
    }
  };

  const handlePaste = (event: ClipboardEvent<HTMLInputElement>) => {
    const pasted = digitsOnly(event.clipboardData.getData("text"));
    if (!pasted) return;
    event.preventDefault();
    const next = pasted.slice(0, PIN_LENGTH);
    commit(next);
    focusBox(next.length >= PIN_LENGTH ? PIN_LENGTH - 1 : next.length);
  };

  return (
    <div
      className={cn("pin-boxes", shaking && "is-shaking")}
      role="group"
      aria-label={label}
      aria-describedby={describedBy}
      data-testid="pin-boxes"
    >
      {Array.from({ length: PIN_LENGTH }, (_, index) => {
        const digit = value[index] ?? "";
        return (
          <input
            key={index}
            ref={(el) => {
              refs.current[index] = el;
            }}
            className={cn("input pin-box", digit && "is-filled", glowing && "is-glow")}
            style={{ "--glow-index": index } as CSSProperties}
            type="text"
            inputMode="numeric"
            pattern="[0-9]*"
            autoComplete={index === 0 ? "one-time-code" : "off"}
            maxLength={index === 0 ? PIN_LENGTH : 1}
            aria-label={`PIN digit ${index + 1} of ${PIN_LENGTH}`}
            aria-invalid={invalid || undefined}
            value={digit}
            disabled={disabled}
            onChange={handleChange(index)}
            onKeyDown={handleKeyDown(index)}
            onPaste={handlePaste}
            onFocus={(e) => e.currentTarget.select()}
          />
        );
      })}
    </div>
  );
}
