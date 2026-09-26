/*
 * The change-password dialog (spec §21.23): current, new, confirm, and the
 * session consequence stated up front rather than discovered afterwards —
 * a password change always ends every other session for that tier at once.
 * Shared by both cards: which mutation it drives, whose current password it
 * asks for and what the consequence reads are all supplied by the caller
 * (`UsersScreen.tsx`); the form itself, its validation and its error mapping
 * are the same either way, since the request body is the same shape.
 */
import { useState, type ReactNode } from "react";
import type { UseMutationResult } from "@tanstack/react-query";

import { ApiError } from "@/api/client";
import { presentError, presentationFor } from "@/api/errors";
import { Banner } from "@/components/ui/Banner";
import { Button } from "@/components/ui/Button";
import { PasswordField } from "@/components/ui/Input";
import { Sheet, SheetContent } from "@/components/ui/Sheet";
import { saveFormOnShortcut } from "@/lib/keyboard";
import { MIN_PASSWORD_LENGTH } from "@/setup/types";

export interface PasswordChangeBody {
  current_password: string;
  new_password: string;
}

interface FormErrors {
  current?: string | undefined;
  next?: string | undefined;
  confirm?: string | undefined;
}

export interface ChangePasswordDialogProps<TResponse> {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  /** "Current password" for a tier's own card, "Your current password" when an admin resets the operator's. */
  currentPasswordLabel: string;
  /** Stated up front, above the fields — who gets signed out, and where (§21.23). */
  consequence: ReactNode;
  mutation: UseMutationResult<TResponse, unknown, PasswordChangeBody>;
  onSuccess: (response: TResponse) => void;
}

export function ChangePasswordDialog<TResponse>({
  open,
  onOpenChange,
  title,
  currentPasswordLabel,
  consequence,
  mutation,
  onSuccess,
}: ChangePasswordDialogProps<TResponse>) {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [errors, setErrors] = useState<FormErrors>({});

  function reset(): void {
    setCurrent("");
    setNext("");
    setConfirm("");
    setErrors({});
  }

  function close(): void {
    reset();
    onOpenChange(false);
  }

  function submit(): void {
    const found: FormErrors = {};
    if (current.length === 0) found.current = "Enter the current password.";
    if (next.length < MIN_PASSWORD_LENGTH) {
      found.next = `The password must be at least ${MIN_PASSWORD_LENGTH} characters.`;
    }
    if (next !== confirm) found.confirm = "The two passwords do not match.";
    setErrors(found);
    if (Object.keys(found).length > 0) return;

    mutation.mutate(
      { current_password: current, new_password: next },
      {
        onSuccess: (response) => {
          onSuccess(response);
          close();
        },
        onError: (error) => {
          if (error instanceof ApiError && error.code === "validation_failed") {
            const presentation = presentationFor(error);
            if (presentation.kind === "inline") {
              const currentMessage = presentation.fields["current_password"];
              const newMessage = presentation.fields["new_password"] ?? presentation.fields["body.new_password"];
              if (currentMessage) {
                setErrors({ current: currentMessage });
                return;
              }
              if (newMessage) {
                setErrors({ next: newMessage });
                return;
              }
            }
          }
          presentError(error);
        },
      },
    );
  }

  return (
    <Sheet
      open={open}
      onOpenChange={(next) => {
        if (next) onOpenChange(next);
        else close();
      }}
    >
      <SheetContent title={title}>
        <form
          className="sheet-body"
          noValidate
          onKeyDown={saveFormOnShortcut}
          onSubmit={(event) => {
            event.preventDefault();
            submit();
          }}
        >
          <Banner tone="info">{consequence}</Banner>
          <PasswordField
            label={currentPasswordLabel}
            helpId="users.dialog.current-password"
            value={current}
            error={errors.current}
            autoComplete="current-password"
            autoFocus
            onChange={(event) => {
              setCurrent(event.currentTarget.value);
              setErrors((prev) => ({ ...prev, current: undefined }));
            }}
          />
          <PasswordField
            label="New password"
            helpId="users.dialog.new-password"
            value={next}
            error={errors.next}
            autoComplete="new-password"
            onChange={(event) => {
              setNext(event.currentTarget.value);
              setErrors((prev) => ({ ...prev, next: undefined }));
            }}
          />
          <PasswordField
            label="Enter it again"
            helpId="users.dialog.confirm-password"
            value={confirm}
            error={errors.confirm}
            autoComplete="new-password"
            onChange={(event) => {
              setConfirm(event.currentTarget.value);
              setErrors((prev) => ({ ...prev, confirm: undefined }));
            }}
          />
          <div className="dialog-actions">
            <Button variant="secondary" onClick={close}>
              Cancel
            </Button>
            <Button type="submit" variant="primary" helpId="users.dialog.submit" loading={mutation.isPending}>
              Change password
            </Button>
          </div>
        </form>
      </SheetContent>
    </Sheet>
  );
}
