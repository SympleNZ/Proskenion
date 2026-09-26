/*
 * Empty, error and loading states (spec §21.27). An empty state always has an
 * illustration, a primary and a secondary message, and an action only if the
 * current tier can resolve it.
 */
import { TriangleAlert, type LucideIcon } from "lucide-react";
import type { ReactNode } from "react";

import { Button } from "./Button";

export interface EmptyStateProps {
  icon: LucideIcon;
  title: string;
  detail: string;
  action?: ReactNode | undefined;
}

export function EmptyState({ icon: Icon, title, detail, action }: EmptyStateProps) {
  return (
    <div className="empty-state" role="status">
      <Icon aria-hidden="true" className="empty-illustration size-12" strokeWidth={1.25} />
      <p className="empty-title">{title}</p>
      <p className="empty-detail">{detail}</p>
      {action}
    </div>
  );
}

export interface ErrorStateProps {
  title: string;
  detail?: string | undefined;
  /** e.g. "503 Service Unavailable" */
  status?: string | undefined;
  onRetry?: (() => void) | undefined;
  onBack?: (() => void) | undefined;
}

/** Full page, when a page cannot render (§21.27). */
export function ErrorState({ title, detail = "The server returned an error.", status, onRetry, onBack }: ErrorStateProps) {
  return (
    <div className="error-state" role="alert">
      <TriangleAlert aria-hidden="true" className="error-icon size-8" />
      <h2 className="text-xl">{title}</h2>
      <p>{detail}</p>
      <p className="text-fg-muted text-sm">Your changes have not been lost.</p>
      <div className="flex gap-3">
        {onRetry ? (
          <Button variant="primary" onClick={onRetry}>
            Try again
          </Button>
        ) : null}
        {onBack ? (
          <Button variant="secondary" onClick={onBack}>
            Go back
          </Button>
        ) : null}
      </div>
      {status ? <p className="error-status">{status}</p> : null}
    </div>
  );
}

/** Skeletons match approximate content shape so nothing jumps (§21.27). */
export function Skeleton({ className }: { className?: string }) {
  return <div className={`skeleton ${className ?? ""}`} aria-hidden="true" />;
}
