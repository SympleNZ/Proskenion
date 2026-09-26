/*
 * Admin → System → Logs → System (spec §21.24 "Logs", §16.7, §4.10): the raw
 * structured application log — `/data/logs/application.log` and its rotated
 * files — with a level filter, a module filter, a date range and an export.
 * Filters, pagination and the newest-first order all come from the server,
 * exactly the shape `SecurityLogViewer` already uses for its own tab.
 */
import { useState } from "react";

import { formatDateTime } from "@/admin/updates/format";
import { ApiError } from "@/api/client";
import { Button } from "@/components/ui/Button";
import { ErrorState, Skeleton } from "@/components/ui/EmptyState";
import { Field, Input } from "@/components/ui/Input";
import { Select } from "@/components/ui/Select";
import { formatRelative } from "@/lib/time";

import { systemLogExportUrl, useSystemLog } from "./api";
import { SYSTEM_LOG_LEVELS, type SystemLogFilter, type SystemLogLevel } from "./types";

const PAGE_SIZE = 50;

function statusLine(error: unknown): string | undefined {
  return error instanceof ApiError ? `${error.status} ${error.code}` : undefined;
}

export function SystemLogViewer() {
  const [level, setLevel] = useState<SystemLogLevel | "">("");
  const [module, setModule] = useState("");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [offset, setOffset] = useState(0);

  const filter: SystemLogFilter = {
    level: level || undefined,
    module: module || undefined,
    from: from || undefined,
    to: to || undefined,
    limit: PAGE_SIZE,
    offset,
  };
  const log = useSystemLog(filter);

  function updateFilter<T>(setter: (value: T) => void) {
    return (value: T) => {
      setter(value);
      setOffset(0);
    };
  }

  const entries = log.data?.entries ?? [];

  return (
    <div className="system-log">
      <div className="system-log-filters">
        <Field label="Level" htmlFor="system-log-level" helpId="logs.system.level" errorId="system-log-level-error">
          <Select id="system-log-level" value={level} onChange={(e) => updateFilter(setLevel)(e.currentTarget.value as SystemLogLevel | "")}>
            <option value="">Any</option>
            {SYSTEM_LOG_LEVELS.map((name) => (
              <option key={name} value={name}>
                {name} and above
              </option>
            ))}
          </Select>
        </Field>
        <Field label="Module" htmlFor="system-log-module" helpId="logs.system.module" errorId="system-log-module-error">
          <Input
            id="system-log-module"
            value={module}
            placeholder="proskenion.core.mixer"
            onChange={(e) => updateFilter(setModule)(e.currentTarget.value)}
          />
        </Field>
        <Field label="From" htmlFor="system-log-from" helpId="logs.system.from" errorId="system-log-from-error">
          <Input id="system-log-from" type="datetime-local" value={from} onChange={(e) => updateFilter(setFrom)(e.currentTarget.value)} />
        </Field>
        <Field label="To" htmlFor="system-log-to" helpId="logs.system.to" errorId="system-log-to-error">
          <Input id="system-log-to" type="datetime-local" value={to} onChange={(e) => updateFilter(setTo)(e.currentTarget.value)} />
        </Field>
        <a
          className="btn btn-secondary"
          href={systemLogExportUrl(filter)}
          download
          aria-label="Export the filtered log as plain text"
        >
          Export
        </a>
      </div>

      {log.isPending ? (
        <>
          <Skeleton className="h-touch w-full" />
          <Skeleton className="h-touch w-full" />
        </>
      ) : null}
      {log.isError ? (
        <ErrorState title="Could not load the system log" status={statusLine(log.error)} onRetry={() => void log.refetch()} />
      ) : null}
      {log.data && entries.length === 0 ? <p className="text-fg-muted text-sm">No log lines match these filters.</p> : null}
      {log.data && entries.length > 0 ? (
        <ul className="system-log-list">
          {entries.map((entry, index) => (
            <li key={`${entry.timestamp}-${index}`} className="system-log-row" data-level={entry.level}>
              <div className="system-log-head">
                <span className="system-log-level" data-level={entry.level}>
                  {entry.level}
                </span>
                <span className="system-log-logger">{entry.logger}</span>
                <span className="technical" title={formatDateTime(entry.timestamp)}>
                  {formatRelative(entry.timestamp)} · {formatDateTime(entry.timestamp)}
                </span>
              </div>
              <p className="system-log-message">{entry.message}</p>
              {Object.keys(entry.context).length > 0 ? (
                <pre className="system-log-context">{JSON.stringify(entry.context, null, 2)}</pre>
              ) : null}
            </li>
          ))}
        </ul>
      ) : null}

      <div className="system-log-pagination">
        <Button variant="secondary" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>
          Newer
        </Button>
        <Button variant="secondary" disabled={!(log.data?.has_more ?? false)} onClick={() => setOffset(offset + PAGE_SIZE)}>
          Older
        </Button>
      </div>
    </div>
  );
}
