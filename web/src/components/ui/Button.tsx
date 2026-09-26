/*
 * Button (spec §21.3 quick reference). Primary, secondary, destructive and
 * ghost; every one defines default, hover, focus, active and disabled in
 * components.css. Minimum 44 px; 64 px for primary operator buttons; 72 px on
 * the hirer surface. Loading keeps the width so nothing shifts (§21.27).
 */
import { cva, type VariantProps } from "class-variance-authority";
import type { ComponentProps } from "react";

import { HelpButton } from "@/help/HelpButton";
import type { HelpId } from "@/help/registry";
import { cn } from "@/lib/utils";

const buttonVariants = cva("btn", {
  variants: {
    variant: {
      primary: "btn-primary",
      secondary: "btn-secondary",
      destructive: "btn-destructive",
      ghost: "btn-ghost",
    },
    size: {
      standard: "",
      primary: "btn-lg",
      hirer: "btn-hirer",
      icon: "btn-icon",
    },
    block: {
      true: "btn-block",
    },
  },
  defaultVariants: {
    variant: "secondary",
    size: "standard",
  },
});

export interface ButtonProps extends Omit<ComponentProps<"button">, "type">, VariantProps<typeof buttonVariants> {
  type?: "button" | "submit" | "reset";
  loading?: boolean;
  /** Brief teal glow after a successful action (§21.8). */
  success?: boolean;
  /** Opens this action's entry in the inline help registry (spec §19.1) next to it. */
  helpId?: HelpId | undefined;
  /**
   * This click opens a `ui/Sheet` `ConfirmDialog` elsewhere in the same
   * component, rather than being wired to a Radix `Dialog.Trigger` — the
   * dialog's `open` prop is state-controlled, so Radix has nothing to wire
   * `aria-haspopup` onto by itself. Setting this both adds that (a real
   * screen reader win: the click does open a dialog) and marks the button
   * `data-confirm-trigger`, which `help/coverage.ts` treats the same as a
   * destructive button — a confirmation gate is exactly the kind of
   * disruptive action (Restart, Reboot) that needs explaining even when it
   * is not itself styled `destructive`.
   */
  confirmTrigger?: boolean | undefined;
}

export function Button({
  className,
  variant,
  size,
  block,
  loading = false,
  success = false,
  disabled,
  type = "button",
  children,
  helpId,
  confirmTrigger,
  ...props
}: ButtonProps) {
  const button = (
    <button
      type={type}
      className={cn(buttonVariants({ variant, size, block: block ? true : undefined }), loading && "is-loading", success && "is-success", className)}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      aria-haspopup={confirmTrigger ? "dialog" : undefined}
      data-confirm-trigger={confirmTrigger ? "" : undefined}
      {...props}
    >
      {children}
    </button>
  );
  if (!helpId) return button;
  return (
    <span className="btn-help-group">
      {button}
      <HelpButton id={helpId} />
    </span>
  );
}
