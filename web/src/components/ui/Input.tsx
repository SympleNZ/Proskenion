/*
 * Input (spec §21.3 quick reference): bg base, hairline border, teal border
 * on focus. Forms never clear on error — what was typed is preserved (§21.27).
 */
import { Eye, EyeOff, TriangleAlert } from "lucide-react";
import { useId, useState, type ComponentProps, type ReactNode, type Ref } from "react";

import { FieldLabel } from "@/help/HelpButton";
import type { HelpId } from "@/help/registry";
import { cn } from "@/lib/utils";

export interface InputProps extends ComponentProps<"input"> {
  ref?: Ref<HTMLInputElement>;
  mono?: boolean;
}

export function Input({ className, mono, ...props }: InputProps) {
  return <input className={cn("input", mono && "input-mono", className)} {...props} />;
}

export interface FieldProps {
  label: ReactNode;
  htmlFor: string;
  error?: string | null | undefined;
  errorId: string;
  children: ReactNode;
  /** Opens this field's entry in the inline help registry (spec §19.1) next to its label. */
  helpId?: HelpId | undefined;
}

/** Label, control and a live error line (§24.3). */
export function Field({ label, htmlFor, error, errorId, children, helpId }: FieldProps) {
  return (
    <div className="field">
      <FieldLabel htmlFor={htmlFor} help={helpId}>
        {label}
      </FieldLabel>
      {children}
      <div className="field-error" id={errorId} role="alert" aria-live="assertive">
        {error ? (
          <>
            <TriangleAlert aria-hidden="true" className="size-4" />
            <span>{error}</span>
          </>
        ) : null}
      </div>
    </div>
  );
}

export interface PasswordFieldProps extends Omit<InputProps, "type"> {
  label?: ReactNode;
  error?: string | null | undefined;
  helpId?: HelpId | undefined;
}

/** One password field with show/hide (§21.8). */
export function PasswordField({ label = "Password", error, id, className, helpId, ...props }: PasswordFieldProps) {
  const generated = useId();
  const inputId = id ?? generated;
  const errorId = `${inputId}-error`;
  const [shown, setShown] = useState(false);
  return (
    <Field label={label} htmlFor={inputId} error={error} errorId={errorId} helpId={helpId}>
      <div className="input-wrap">
        <Input
          id={inputId}
          type={shown ? "text" : "password"}
          aria-invalid={error ? true : undefined}
          aria-describedby={error ? errorId : undefined}
          className={className}
          {...props}
        />
        <button
          type="button"
          className="btn btn-ghost btn-icon input-affix"
          aria-label={shown ? "Hide password" : "Show password"}
          aria-pressed={shown}
          onClick={() => setShown((s) => !s)}
          tabIndex={0}
        >
          {shown ? <EyeOff aria-hidden="true" className="size-5" /> : <Eye aria-hidden="true" className="size-5" />}
        </button>
      </div>
    </Field>
  );
}
