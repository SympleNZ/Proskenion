/*
 * Route guards (spec §6.13). Unauthenticated → /login. A hirer only ever sees
 * /hire — the existence of /admin is not exposed to them, they are simply
 * sent home. Staff at /hire go to /app. An operator at /admin gets the
 * "not permitted" state.
 */
import type { ReactNode } from "react";
import { Navigate, useLocation } from "react-router-dom";

import { homeFor, type Tier } from "@/api/auth";
import { Skeleton } from "@/components/ui/EmptyState";
import { useSession } from "@/session/context";

export interface RequireTierProps {
  allow: readonly Tier[];
  /** Shown to a signed-in staff member whose tier is not allowed here. */
  denied?: ReactNode;
  children: ReactNode;
}

export function RequireTier({ allow, denied, children }: RequireTierProps) {
  const { status, session } = useSession();
  const location = useLocation();

  if (status === "loading") {
    return (
      <div className="centre" aria-busy="true" aria-label="Loading">
        <Skeleton className="h-touch-primary w-full" />
      </div>
    );
  }
  if (status !== "authenticated" || !session) {
    return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  }
  if (allow.includes(session.tier)) return <>{children}</>;

  const wantsHirerSurface = allow.length === 1 && allow[0] === "hirer";
  if (session.tier === "hirer" || wantsHirerSurface) {
    return <Navigate to={homeFor(session.tier)} replace />;
  }
  return <>{denied ?? <Navigate to={homeFor(session.tier)} replace />}</>;
}
