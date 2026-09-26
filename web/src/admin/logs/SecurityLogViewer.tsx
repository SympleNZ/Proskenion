/*
 * Admin → System → Logs → Security (spec §6.14, §21.24): the `security_events`
 * audit trail — who signed in, what was denied, what changed. Filters (event
 * type, outcome, client IP, a time range) and offset pagination follow the
 * pattern `GET /scenes/log` and its viewer already use; newest first, both
 * ways, is the server's own default. Every value is what the server already
 * redacted (`proskenion/api/system.py`) — nothing here filters again.
 */
import { useState } from "react";

import { formatDateTime } from "@/admin/updates/format";
import { ApiError } from "@/api/client";
import { Button } from "@/components/ui/Button";
import { ErrorState, Skeleton } from "@/components/ui/EmptyState";
import { Field, Input } from "@/components/ui/Input";
import { Select } from "@/components/ui/Select";
import { formatRelative } from "@/lib/time";

import { useSecurityLog } from "./api";
import { SECURITY_EVENT_TYPES, type SecurityLogFilter, type SecurityOutcome } from "./types";

const PAGE_SIZE = 25;

function eventTypeLabel(eventType: string): string {
  return eventType.replace(/_/g, " ");
}

function statusLine(error: unknown): string | undefined {
  return error instanceof ApiError ? `${error.status} ${error.code}` : undefined;
}

export function SecurityLogViewer() {
  const [eventType, setEventType] = useState("");
  const [outcome, setOutcome] = useState<SecurityOutcome | "">("");
  const [ipAddress, setIpAddress] = useState("");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [offset, setOffset] = useState(0);

  const filter: SecurityLogFilter = {
    eventType: eventType || undefined,
    outcome: outcome || undefined,
    ipAddress: ipAddress || undefined,
    from: from || undefined,
    to: to || undefined,
    limit: PAGE_SIZE,
    offset,
  };
  const log = useSecurityLog(filter);

  function updateFilter(setter: (value: string) => void) {
    return (value: string) => {
      setter(value);
      setOffset(0);
    };
  }

  const entries = log.data?.entries ?? [];

  return (
    <div className="security-log">
      <div className="security-log-filters">
        <Field label="Event type" htmlFor="security-log-event-type" helpId="logs.security.event-type" errorId="security-log-event-type-error">
          <Select
            id="security-log-event-type"
            value={eventType}
            onChange={(e) => updateFilter(setEventType)(e.currentTarget.value)}
          >
            <option value="">Any</option>
            {SECURITY_EVENT_TYPES.map((type) => (
              <option key={type} value={type}>
                {eventTypeLabel(type)}
              </option>
            ))}
          </Select>
        </Field>
        <Field label="Outcome" htmlFor="security-log-outcome" helpId="logs.security.outcome" errorId="security-log-outcome-error">
          <Select
            id="security-log-outcome"
            value={outcome}
            onChange={(e) => updateFilter((v) => setOutcome(v as SecurityOutcome | ""))(e.currentTarget.value)}
          >
            <option value="">Any</option>
            <option value="success">Success</option>
            <option value="failure">Failure</option>
          </Select>
        </Field>
        <Field label="Client IP" htmlFor="security-log-ip" helpId="logs.security.ip" errorId="security-log-ip-error">
          <Input
            id="security-log-ip"
            value={ipAddress}
            placeholder="10.2.30.x"
            onChange={(e) => updateFilter(setIpAddress)(e.currentTarget.value)}
          />
        </Field>
        <Field label="From" htmlFor="security-log-from" helpId="logs.security.from" errorId="security-log-from-error">
          <Input
            id="security-log-from"
            type="datetime-local"
            value={from}
            onChange={(e) => updateFilter(setFrom)(e.currentTarget.value)}
          />
        </Field>
        <Field label="To" htmlFor="security-log-to" helpId="logs.security.to" errorId="security-log-to-error">
          <Input
            id="security-log-to"
            type="datetime-local"
            value={to}
            onChange={(e) => updateFilter(setTo)(e.currentTarget.value)}
          />
        </Field>
      </div>

      {log.isPending ? (
        <>
          <Skeleton className="h-touch w-full" />
          <Skeleton className="h-touch w-full" />
        </>
      ) : null}
      {log.isError ? (
        <ErrorState title="Could not load the security log" status={statusLine(log.error)} onRetry={() => void log.refetch()} />
      ) : null}
      {log.data && entries.length === 0 ? <p className="text-fg-muted text-sm">No events match these filters.</p> : null}
      {log.data && entries.length > 0 ? (
        <ul className="security-log-list">
          {entries.map((entry) => (
            <li key={entry.id} className="security-log-row" data-outcome={entry.outcome}>
              <div className="security-log-head">
                <span className="security-log-outcome" data-outcome={entry.outcome}>
                  {entry.outcome === "failure" ? "✕" : "✓"} {eventTypeLabel(entry.event_type)}
                </span>
                <span className="technical" title={formatDateTime(entry.timestamp)}>
                  {formatRelative(entry.timestamp)} · {formatDateTime(entry.timestamp)}
                </span>
              </div>
              <div className="security-log-meta">
                <span>{entry.user_ident ?? "—"}</span>
                <span>{entry.ip_address ?? "—"}</span>
              </div>
              {entry.detail && Object.keys(entry.detail).length > 0 ? (
                <pre className="security-log-detail">{JSON.stringify(entry.detail, null, 2)}</pre>
              ) : null}
            </li>
          ))}
        </ul>
      ) : null}

      <div className="security-log-pagination">
        <Button variant="secondary" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>
          Newer
        </Button>
        <Button variant="secondary" disabled={entries.length < PAGE_SIZE} onClick={() => setOffset(offset + PAGE_SIZE)}>
          Older
        </Button>
      </div>
    </div>
  );
}
