/*
 * Admin → Users (spec §21.23): two fixed cards, Admin and Operator. No
 * creating, no deleting. Each tier changes its own password from its own
 * card; the admin can also reset the operator's from the operator card —
 * `proskenion/api/auth.py`'s module docstring records the whose-current-
 * password contract this follows.
 */
import { useState } from "react";
import { toast } from "sonner";

import { ApiError } from "@/api/client";
import { Banner } from "@/components/ui/Banner";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { ErrorState, Skeleton } from "@/components/ui/EmptyState";
import { useSession } from "@/session/context";

import { useChangeOperatorPassword, useChangeOwnPassword, usePasswordStatus } from "./api";
import { ChangePasswordDialog } from "./ChangePasswordDialog";
import type { ChangeOperatorPasswordResponse, ChangeOwnPasswordResponse } from "./types";

function statusLine(error: unknown): string | undefined {
  return error instanceof ApiError ? `${error.status} ${error.code}` : undefined;
}

/** "8 April 2026", or "Not recorded" — the seed placeholder has never been replaced (§21.23). */
function formatChangedAt(iso: string | null): string {
  if (!iso) return "Not recorded";
  const at = Date.parse(iso);
  if (Number.isNaN(at)) return "Not recorded";
  return new Date(at).toLocaleDateString("en-NZ", { day: "numeric", month: "long", year: "numeric" });
}

export function UsersScreen() {
  const statusQuery = usePasswordStatus();
  const { signIn } = useSession();
  const changeOwn = useChangeOwnPassword();
  const changeOperator = useChangeOperatorPassword();

  const [adminDialogOpen, setAdminDialogOpen] = useState(false);
  const [operatorDialogOpen, setOperatorDialogOpen] = useState(false);

  if (statusQuery.isPending) {
    return (
      <div className="view users-view" aria-busy="true" aria-label="Loading account status">
        <h1 className="view-title">Users</h1>
        <Skeleton className="h-touch w-full" />
        <Skeleton className="h-touch w-full" />
      </div>
    );
  }

  if (statusQuery.isError || !statusQuery.data) {
    return (
      <div className="view users-view">
        <h1 className="view-title">Users</h1>
        <ErrorState
          title="Could not load the account status"
          status={statusLine(statusQuery.error)}
          onRetry={() => void statusQuery.refetch()}
        />
      </div>
    );
  }

  const status = statusQuery.data;

  return (
    <div className="view users-view">
      <header className="view-head">
        <div>
          <h1 className="view-title">Users</h1>
          <p className="view-lede">Two fixed staff accounts. No creating, no deleting (§21.23).</p>
        </div>
      </header>

      {status.identical ? (
        <Banner tone="info" title="The admin and operator passwords are the same">
          That is a legitimate choice, not an error.
        </Banner>
      ) : null}

      <div className="grid grid-cols-2 gap-4">
        <Card className="device-card" title="Admin" titleLevel="h2">
          <p className="text-fg-secondary">Full access — configuration and control</p>
          <p>Password last changed: {formatChangedAt(status.admin.password_changed_at)}</p>
          <Button variant="secondary" helpId="users.admin.change" onClick={() => setAdminDialogOpen(true)}>
            Change password
          </Button>
        </Card>

        <Card className="device-card" title="Operator" titleLevel="h2">
          <p className="text-fg-secondary">Control only — no configuration access</p>
          <p>Password last changed: {formatChangedAt(status.operator.password_changed_at)}</p>
          <Button variant="secondary" helpId="users.operator.change" onClick={() => setOperatorDialogOpen(true)}>
            Change password
          </Button>
        </Card>
      </div>

      <Banner tone="info">
        The sign-in page has a single password field. Whichever password matches determines the access level. If both
        are the same, admin applies.
      </Banner>

      <Banner tone="info" title="Forgotten password?">
        Reset requires SSH access — see the recovery documentation. There is no reset link, deliberately.
      </Banner>

      <ChangePasswordDialog<ChangeOwnPasswordResponse>
        open={adminDialogOpen}
        onOpenChange={setAdminDialogOpen}
        title="Change the admin password"
        currentPasswordLabel="Current password"
        consequence="You'll be signed out on other devices — this one stays signed in."
        mutation={changeOwn}
        onSuccess={(response) => {
          signIn({ tier: response.tier, expires_at: response.expires_at });
          toast.success("Admin password changed.");
        }}
      />

      <ChangePasswordDialog<ChangeOperatorPasswordResponse>
        open={operatorDialogOpen}
        onOpenChange={setOperatorDialogOpen}
        title="Change the operator password"
        currentPasswordLabel="Your current password"
        consequence="Signs the operator out on every other device. Needs your own admin password, not the operator's."
        mutation={changeOperator}
        onSuccess={() => {
          toast.success("Operator password changed.");
        }}
      />
    </div>
  );
}
