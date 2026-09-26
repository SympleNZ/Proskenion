/*
 * The execution log viewer (spec §8.16, §16.5, §21.16): every run, its
 * trigger, its result, and what each action did. Retained 90 days server
 * side; this only ever shows a page of it. `sceneId` narrows to one scene's
 * history; omitted, it is every scene's — the same component either way.
 */
import { ErrorState, Skeleton } from "@/components/ui/EmptyState";
import { Sheet, SheetContent } from "@/components/ui/Sheet";
import { formatRelative } from "@/lib/time";
import { ApiError } from "@/api/client";

import { useSceneLog } from "./api";

export interface LogViewerProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  sceneId?: number | undefined;
}

function resultWord(result: string | null): string {
  return result ?? "in progress";
}

export function LogViewer({ open, onOpenChange, sceneId }: LogViewerProps) {
  const log = useSceneLog({ sceneId });

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent title="Execution log" description="Every run, newest first. Retained 90 days (§8.16).">
        {log.isPending ? (
          <>
            <Skeleton className="h-touch w-full" />
            <Skeleton className="h-touch w-full" />
          </>
        ) : null}
        {log.isError ? (
          <ErrorState
            title="Could not load the log"
            status={log.error instanceof ApiError ? `${log.error.status} ${log.error.code}` : undefined}
            onRetry={() => void log.refetch()}
          />
        ) : null}
        {log.data && log.data.entries.length === 0 ? <p className="text-fg-muted text-sm">No runs recorded yet.</p> : null}
        {log.data && log.data.entries.length > 0 ? (
          <ul className="scene-log-list">
            {log.data.entries.map((entry) => (
              <li key={entry.id} className="scene-log-row" data-result={entry.result ?? "running"}>
                <div className="scene-log-head">
                  <span>{entry.triggered_by}</span>
                  <span>{resultWord(entry.result)}</span>
                  <span className="technical">{formatRelative(entry.started_at)}</span>
                </div>
                {entry.action_results.length > 0 ? (
                  <ul className="scene-log-actions">
                    {entry.action_results.map((action, index) => (
                      <li key={index}>
                        {String(action["marker"] ?? "")} {String(action["domain"] ?? "")} — {String(action["reason"] ?? action["result"] ?? "")}
                      </li>
                    ))}
                  </ul>
                ) : null}
              </li>
            ))}
          </ul>
        ) : null}
      </SheetContent>
    </Sheet>
  );
}
