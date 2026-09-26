/*
 * Admin → System → Logs → DEBUG logging (spec §4.10): one switch per
 * top-level module. Each toggle is its own `PUT /system/debug-logging`,
 * exactly like the hirer kill switch (`PinAccessCard.tsx`) — it takes
 * effect the moment the request answers, live, without a restart, so there
 * is no separate "save".
 */
import { ApiError } from "@/api/client";
import { presentError } from "@/api/errors";
import { Card } from "@/components/ui/Card";
import { ErrorState, Skeleton } from "@/components/ui/EmptyState";
import { Checkbox } from "@/components/ui/Select";
import { HelpButton } from "@/help/HelpButton";

import { useDebugLogging, useSetDebugLogging } from "./api";

function statusLine(error: unknown): string | undefined {
  return error instanceof ApiError ? `${error.status} ${error.code}` : undefined;
}

/** "proskenion.core" → "core". */
function shortName(name: string): string {
  return name.startsWith("proskenion.") ? name.slice("proskenion.".length) : name;
}

// One help affordance for the card as a whole, rather than one per checkbox —
// every row does the same thing (turn DEBUG on for that module), so a single
// explanation covers all of them (spec §19.1; `help/coverage.ts` requires at
// least one help trigger somewhere in a card that has any control).
const CARD_TITLE = (
  <span className="flex items-center gap-2">
    Debug logging
    <HelpButton id="logs.debug-logging" label="Debug logging" />
  </span>
);

export function DebugLoggingCard() {
  const loggers = useDebugLogging();
  const setLogging = useSetDebugLogging();

  if (loggers.isPending) {
    return (
      <Card className="device-card" title={CARD_TITLE} titleLevel="h2">
        <div aria-busy="true" aria-label="Loading loggers">
          <Skeleton className="h-touch w-full" />
        </div>
      </Card>
    );
  }
  if (loggers.isError || !loggers.data) {
    return (
      <Card className="device-card" title={CARD_TITLE} titleLevel="h2">
        <ErrorState title="Could not load the logger list" status={statusLine(loggers.error)} onRetry={() => void loggers.refetch()} />
      </Card>
    );
  }

  return (
    <Card className="device-card" title={CARD_TITLE} titleLevel="h2">
      <p className="field-help">
        INFO is the default for every module. Turning DEBUG on here takes effect immediately, with no restart, and reverts the
        same way.
      </p>
      <ul className="debug-logging-list">
        {loggers.data.loggers.map((row) => (
          <li key={row.name} className="debug-logging-row">
            <Checkbox
              id={`debug-logging-${row.name}`}
              label={shortName(row.name)}
              checked={row.enabled}
              disabled={setLogging.isPending}
              onChange={(e) =>
                setLogging.mutate(
                  { logger: row.name, enabled: e.currentTarget.checked },
                  { onError: (error) => presentError(error) },
                )
              }
            />
          </li>
        ))}
      </ul>
    </Card>
  );
}
