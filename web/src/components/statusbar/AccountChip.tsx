/*
 * The account chip (spec §21.7): the only control in the bar's right end on
 * operator and admin views. A short menu — who is signed in and the tier;
 * Admin (admin only — a menu item, not a tab); Change password (operator
 * only — the admin's own lives on Admin → Users instead, §21.23); Display
 * scale (only at the design target, §21.9); Log out.
 */
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { toast } from "sonner";

import { useChangeOwnPassword } from "@/admin/users/api";
import { ChangePasswordDialog } from "@/admin/users/ChangePasswordDialog";
import type { ChangeOwnPasswordResponse } from "@/admin/users/types";
import { TIER_LABELS, type Tier } from "@/api/auth";
import { Menu, MenuContent, MenuItem, MenuLabel, MenuSeparator, MenuTrigger } from "@/components/ui/Menu";
import { useAtDesignTarget } from "@/lib/viewport";
import { useSession } from "@/session/context";

export function AccountChip({ tier }: { tier: Tier }) {
  const navigate = useNavigate();
  const { signIn, signOut } = useSession();
  const atDesignTarget = useAtDesignTarget();
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
          {atDesignTarget && (
            <MenuItem onSelect={() => toast.info("Display scale is coming in a later task")}>Display scale</MenuItem>
          )}
          {(tier === "admin" || tier === "operator" || atDesignTarget) && <MenuSeparator />}
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
