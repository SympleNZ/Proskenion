/*
 * The account chip (spec §21.7): the only control in the bar's right end on
 * operator and admin views. A short menu — who is signed in and the tier;
 * Admin (admin only — a menu item, not a tab); Change password (operator
 * only — the admin's own lives on Admin → Users instead, §21.23); Display
 * scale (only at or above the design target, §21.9); Log out.
 *
 * Display scale is a stepper in tenths (1.0×–2.0×) whose steps keep the
 * menu open: the factor applies app-wide the moment it changes
 * (`lib/useDisplayScale.ts`), so the operator sees the result while
 * choosing. The hirer never reaches this component (`HirerLogout`).
 */
import { Minus, Plus } from "lucide-react";
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { toast } from "sonner";

import { useChangeOwnPassword } from "@/admin/users/api";
import { ChangePasswordDialog } from "@/admin/users/ChangePasswordDialog";
import type { ChangeOwnPasswordResponse } from "@/admin/users/types";
import { TIER_LABELS, type Tier } from "@/api/auth";
import { Menu, MenuContent, MenuItem, MenuLabel, MenuSeparator, MenuTrigger } from "@/components/ui/Menu";
import { DISPLAY_SCALE_MAX, DISPLAY_SCALE_MIN, DISPLAY_SCALE_STEP } from "@/lib/displayScale";
import { setDisplayScale, useDisplayScale } from "@/lib/useDisplayScale";
import { useSession } from "@/session/context";

/** The factor as the menu shows it: one decimal and a multiplication sign. */
function formatScale(scale: number): string {
  return `${scale.toFixed(1)}×`;
}

function DisplayScaleItems() {
  const { scale } = useDisplayScale();
  const step = (direction: 1 | -1) => (event: Event) => {
    // Keep the menu open: each tenth is judged by looking at the result.
    event.preventDefault();
    setDisplayScale(scale + direction * DISPLAY_SCALE_STEP);
  };
  return (
    <div className="menu-scale" role="group" aria-labelledby="account-display-scale-label">
      <span className="menu-scale-label" id="account-display-scale-label">
        Display scale
      </span>
      <MenuItem
        className="menu-scale-step"
        aria-label="Smaller display scale"
        disabled={scale <= DISPLAY_SCALE_MIN}
        onSelect={step(-1)}
      >
        <Minus aria-hidden="true" className="size-4" />
      </MenuItem>
      <output className="menu-scale-value" aria-live="polite" data-testid="display-scale-value">
        {formatScale(scale)}
      </output>
      <MenuItem
        className="menu-scale-step"
        aria-label="Larger display scale"
        disabled={scale >= DISPLAY_SCALE_MAX}
        onSelect={step(1)}
      >
        <Plus aria-hidden="true" className="size-4" />
      </MenuItem>
    </div>
  );
}

export function AccountChip({ tier }: { tier: Tier }) {
  const navigate = useNavigate();
  const { signIn, signOut } = useSession();
  const { available: scaleAvailable } = useDisplayScale();
  const label = TIER_LABELS[tier];
  const changeOwn = useChangeOwnPassword();
  const [passwordDialogOpen, setPasswordDialogOpen] = useState(false);

  return (
    <>
      <Menu modal={false}>
        <MenuTrigger asChild>
          <button type="button" className="account-chip" aria-label={`Account: ${label}`}>
            {label.charAt(0)}
          </button>
        </MenuTrigger>
        <MenuContent align="end" side="top">
          <MenuLabel>{label}</MenuLabel>
          <MenuSeparator />
          {tier === "admin" && <MenuItem onSelect={() => navigate("/admin")}>Admin</MenuItem>}
          {tier === "operator" && (
            <MenuItem onSelect={() => setPasswordDialogOpen(true)}>Change password</MenuItem>
          )}
          {scaleAvailable && <DisplayScaleItems />}
          {(tier === "admin" || tier === "operator" || scaleAvailable) && <MenuSeparator />}
          <MenuItem onSelect={() => void signOut()}>Log out</MenuItem>
        </MenuContent>
      </Menu>
      {tier === "operator" && (
        <ChangePasswordDialog<ChangeOwnPasswordResponse>
          open={passwordDialogOpen}
          onOpenChange={setPasswordDialogOpen}
          title="Change your password"
          currentPasswordLabel="Current password"
          consequence="You'll be signed out on other devices — this one stays signed in."
          mutation={changeOwn}
          onSuccess={(response) => {
            signIn({ tier: response.tier, expires_at: response.expires_at });
            toast.success("Password changed.");
          }}
        />
      )}
    </>
  );
}
