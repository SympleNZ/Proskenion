/*
 * Sheet and confirm dialog over Radix Dialog (shadcn/ui underneath). Focus is
 * trapped, Escape closes, and focus returns to the trigger (§24.3). A sheet
 * is a bottom sheet on mobile and a side panel from the tablet breakpoint.
 *
 * Radix's own close-focus behaviour only refocuses a `Dialog.Trigger`, and
 * almost every sheet (about 28) and every `ConfirmDialog` (33 files) is
 * opened from a controlled `open` prop set by an ordinary button, with no
 * Trigger — so without help, focus drops to `<body>` on close.
 * `useReturnFocus` below fixes this centrally, and deliberately does not
 * lean on Radix's own `onOpenAutoFocus`/`FocusScope`'s `onMountAutoFocus` to
 * capture the opener: that event only fires when nothing inside the panel is
 * focused yet, and a field with `autoFocus` (`ChangePasswordDialog` has one)
 * wins that race in a real browser — React applies `autoFocus` during
 * commit, strictly before any effect (Radix's included) runs. Found by the
 * real-browser e2e check this fix added (tests/e2e/users.spec.ts), which a
 * jsdom render did not reproduce.
 *
 * A React ref or render-time read cannot win that race either: writing to a
 * ref during render is itself unsafe (and this project's lint enforces it),
 * and by the time any effect (Radix's own included) runs, the DOM has
 * already committed and any `autoFocus` has already applied. So this tracks
 * the last real `focusin` outside every currently open dialog in one
 * module-level listener, entirely outside React's render cycle — a genuine
 * button click fires `focusin` on the button synchronously, before React
 * even sees the click, and an `autoFocus` field's own `focusin` is excluded
 * because it fires inside the dialog that has just opened around it. This
 * also covers a genuine `Dialog.Trigger` (a handful of call sites use one
 * instead of a controlled `open` prop) the same way, uniformly.
 *
 * One remaining gap: a dialog opened from a button inside another,
 * already-open dialog falls back to the landmark below rather than the
 * inner button, because that button is itself "inside a dialog" — a rare
 * shape today, and still a sensible fallback rather than a regression.
 */
import { X } from "lucide-react";
import { Dialog } from "radix-ui";
import { useCallback } from "react";
import type { ComponentProps, ReactNode } from "react";

import { cn } from "@/lib/utils";

import { Button } from "./Button";

export const Sheet = Dialog.Root;
export const SheetTrigger = Dialog.Trigger;
export const SheetClose = Dialog.Close;

/** The most recent element focused outside every currently open dialog — see the module doc above. */
let lastFocusOutsideDialog: HTMLElement | null = null;

if (typeof document !== "undefined") {
  document.addEventListener("focusin", (event) => {
    const target = event.target;
    if (target instanceof HTMLElement && !target.closest('[role="dialog"], [role="alertdialog"]')) {
      lastFocusOutsideDialog = target;
    }
  });
}

interface ReturnFocusHandlers {
  onCloseAutoFocus(event: Event): void;
}

/** §24.3, §24.7: closing a sheet or dialog returns focus to whatever opened it. */
function useReturnFocus(): ReturnFocusHandlers {
  const onCloseAutoFocus = useCallback((event: Event) => {
    const opener = lastFocusOutsideDialog;
    if (!opener) return;
    event.preventDefault();
    if (document.contains(opener)) {
      opener.focus();
      return;
    }
    // The opener is gone (e.g. the row it belonged to was just deleted).
    // Fall back to the shell's main landmark (`Shell.tsx`'s `#main`, focusable
    // for exactly this and the skip link).
    document.getElementById("main")?.focus();
  }, []);

  return { onCloseAutoFocus };
}

export interface SheetContentProps extends Omit<ComponentProps<typeof Dialog.Content>, "title"> {
  side?: "right" | "left";
  title: ReactNode;
  description?: ReactNode;
  /** Hide the visual title but keep it for assistive technology. */
  hideTitle?: boolean;
}

export function SheetContent({ side = "right", title, description, hideTitle, className, children, ...props }: SheetContentProps) {
  const returnFocus = useReturnFocus();
  return (
    <Dialog.Portal>
      <Dialog.Overlay className="sheet-backdrop" />
      <Dialog.Content
        className={cn("sheet-content", className)}
        data-side={side}
        onCloseAutoFocus={returnFocus.onCloseAutoFocus}
        {...props}
      >
        <div className={cn("sheet-header", hideTitle && "sr-only")}>
          <div className="flex flex-col gap-1">
            <Dialog.Title className="sheet-title">{title}</Dialog.Title>
            {description ? (
              <Dialog.Description className="sheet-description">{description}</Dialog.Description>
            ) : (
              <Dialog.Description className="sr-only">{title}</Dialog.Description>
            )}
          </div>
          {!hideTitle && (
            <Dialog.Close asChild>
              <Button variant="ghost" size="icon" aria-label="Close">
                <X aria-hidden="true" className="size-5" />
              </Button>
            </Dialog.Close>
          )}
        </div>
        {children}
      </Dialog.Content>
    </Dialog.Portal>
  );
}

export interface ConfirmDialogProps {
  open: boolean;
  onOpenChange(open: boolean): void;
  title: ReactNode;
  description: ReactNode;
  confirmLabel: ReactNode;
  cancelLabel?: ReactNode;
  destructive?: boolean;
  onConfirm(): void;
}

/** Confirmation that says what will happen, not "are you sure" (§21.7). */
export function ConfirmDialog({
  open,
  onOpenChange,
  title,
  description,
  confirmLabel,
  cancelLabel = "Cancel",
  destructive,
  onConfirm,
}: ConfirmDialogProps) {
  const returnFocus = useReturnFocus();
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="dialog-backdrop" />
        <Dialog.Content className="dialog-content" role="alertdialog" onCloseAutoFocus={returnFocus.onCloseAutoFocus}>
          <Dialog.Title className="sheet-title">{title}</Dialog.Title>
          <Dialog.Description className="text-fg-secondary">{description}</Dialog.Description>
          <div className="dialog-actions">
            <Dialog.Close asChild>
              <Button variant="secondary">{cancelLabel}</Button>
            </Dialog.Close>
            <Button variant={destructive ? "destructive" : "primary"} onClick={onConfirm}>
              {confirmLabel}
            </Button>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
