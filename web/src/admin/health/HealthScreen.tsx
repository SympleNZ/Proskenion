/*
 * The Health screen (spec §21.24 *Health*, §11.1, §11.2).
 *
 * Cards for CPU, memory, storage per partition, backup media and the
 * application. Every value comes through the platform layer, so an
 * unsupported metric reads "not available" rather than a wrong number, and
 * every level is paired with a word as well as a colour (§24.1).
 *
 * The device list repeats the operator status bar's information in its
 * detailed form, and adds the two things that bar deliberately leaves out:
 * backup media and the control surface (§10.5).
 */
import { HeartPulse } from "lucide-react";
import type { ReactNode } from "react";

import { ApiError } from "@/api/client";
import { Banner } from "@/components/ui/Banner";
import { Card } from "@/components/ui/Card";
import { EmptyState, ErrorState, Skeleton } from "@/components/ui/EmptyState";
import { LevelBadge, LevelDot } from "@/components/ui/LevelDot";
import type { Level } from "@/components/ui/levels";
import { StatusDot } from "@/components/ui/StatusDot";

import { HEALTH_REFRESH_MS, useHealth } from "./api";
import {
  formatAbsence,
  formatBytes,
  formatCount,
  formatMilliseconds,
  formatPercent,
  formatTemperature,
  formatUptime,
  formatUsage,
  NOT_AVAILABLE,
} from "./format";
import type { Health } from "./types";

function MetricRow({
  label,
  value,
  level,
  note,
}: {
  label: string;
  value: string;
  level?: Level | undefined;
  note?: ReactNode;
}) {
  return (
    <div className="metric-row" data-metric={label}>
      <span className="metric-label">
        {level ? <LevelDot level={level} subject={label} /> : null}
        <span>{label}</span>
      </span>
      <span className="metric-value technical" data-unavailable={value === NOT_AVAILABLE ? "true" : undefined}>
        {value}
      </span>
      {note ? <span className="metric-note">{note}</span> : null}
    </div>
  );
}

function HealthBody({ health }: { health: Health }) {
  const { cpu, memory, storage, application, backup_media: backup, time } = health;

  return (
    <>
      <Card className="health-card" title="Controller" titleLevel="h2">
        <div className="health-overall">
          <LevelBadge level={health.overall} subject="Overall health" />
        </div>
        <MetricRow label="Platform" value={health.platform || NOT_AVAILABLE} />
        <MetricRow label="Version" value={health.version || NOT_AVAILABLE} />
        <MetricRow label="Uptime" value={formatUptime(health.uptime_seconds)} />
        <MetricRow
          label="Time"
          value={time.server_time ?? NOT_AVAILABLE}
          level={time.degraded ? "amber" : time.synced ? "green" : "unknown"}
          note={time.synced ? "synchronised" : "not synchronised — timestamps may drift"}
        />
      </Card>

      <Card className="health-card" title="CPU" titleLevel="h2">
        <MetricRow label="Temperature" value={formatTemperature(cpu.temperature_c)} level={cpu.level} />
      </Card>

      <Card className="health-card" title="Memory" titleLevel="h2">
        <MetricRow label="Used" value={formatUsage(memory.used_bytes, memory.total_bytes)} level={memory.level} />
        <MetricRow label="In use" value={formatPercent(memory.percent)} />
      </Card>

      <Card className="health-card" title={`Storage${storage.model ? ` — ${storage.model}` : ""}`} titleLevel="h2">
        {storage.partial ? (
          <Banner tone="warning" title="Only part of the drive data could be read">
            What is shown is what the platform layer could read. The rest is not available rather than assumed.
          </Banner>
        ) : null}
        <MetricRow
          label="Health"
          value={storage.health ?? NOT_AVAILABLE}
          level={storage.level}
          note={storage.life_used_percent === null ? undefined : `${formatPercent(storage.life_used_percent)} life used`}
        />
        <MetricRow label="Temperature" value={formatTemperature(storage.temperature_c)} />
        <MetricRow label="Media errors" value={formatCount(storage.media_errors)} />
        <h3 className="sect-label">Partitions</h3>
        {health.partitions.length === 0 ? (
          <p className="metric-note">{NOT_AVAILABLE}</p>
        ) : (
          health.partitions.map((partition) => (
            <MetricRow
              key={partition.mount}
              label={partition.mount}
              value={formatUsage(partition.used_bytes, partition.total_bytes)}
              level={partition.level}
              note={
                <>
                  {partition.slot ? `slot ${partition.slot} · ` : ""}
                  {formatBytes(partition.free_bytes)} free
                </>
              }
            />
          ))
        )}
      </Card>

      <Card className="health-card" title="Backup media" titleLevel="h2">
        <MetricRow
          label="Medium"
          value={backup.present ? "connected" : "absent"}
          level={backup.level}
          note={
            backup.present
              ? "amber, never red — a missing backup medium degrades maintenance, not the room"
              : (formatAbsence(backup.absent_since) ?? "absent")
          }
        />
      </Card>

      <Card className="health-card" title="Application" titleLevel="h2">
        <MetricRow
          label="Event loop lag"
          value={`p50 ${formatMilliseconds(application.loop_lag_p50_ms)} · p99 ${formatMilliseconds(application.loop_lag_p99_ms)}`}
          level={application.level}
        />
        <MetricRow label="WebSocket clients" value={formatCount(application.clients)} />
        <MetricRow
          label="Event bus drops"
          value={formatCount(application.bus.drop_count_window)}
          level={application.bus.level}
          note={`${formatCount(application.bus.drop_consecutive_windows)} consecutive windows — shedding is visible, never silent`}
        />
        {application.bus.unsubscribed.length > 0 ? (
          <Banner tone="warning" title="Subscribers removed after repeated failures">
            {application.bus.unsubscribed.join(", ")}. Ten consecutive failures unsubscribe a callback, and that is
            reported here rather than being swallowed.
          </Banner>
        ) : null}
      </Card>

      <Card className="health-card" title="Devices" titleLevel="h2">
        {health.devices.length === 0 ? (
          <p className="metric-note">No devices are configured.</p>
        ) : (
          <ul className="health-devices">
            {health.devices.map((device) => (
              <li className="health-device" key={device.key}>
                <span className="health-device-name">
                  <StatusDot status={device.status} subject={device.name} />
                  <span>{device.name}</span>
                </span>
                <span className="technical">
                  {device.protocol ?? device.category}
                  {device.host ? ` ${device.host}` : ""}
                  {device.port ? `:${device.port}` : ""}
                </span>
                <span className="metric-note">
                  {device.detail ?? device.last_error ?? (device.last_seen ? `last seen ${device.last_seen}` : "")}
                  {device.kind === "config"
                    ? " — check the address, path or permissions"
                    : device.kind === "device"
                      ? " — the transport opened but the device did not reply; check the cable, power or device state"
                      : ""}
                </span>
                <span className="technical">
                  {device.latency_ms === null ? NOT_AVAILABLE : formatMilliseconds(device.latency_ms)} ·{" "}
                  {formatCount(device.reconnects)} reconnects
                </span>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </>
  );
}

export function HealthScreen() {
  const health = useHealth();

  return (
    <div className="view health-view">
      <header className="view-head">
        <div>
          <h1 className="view-title">Health</h1>
          <p className="view-lede">
            Vitals come through the platform layer, so an unsupported metric reads &ldquo;not available&rdquo; rather
            than a wrong number. Refreshes every {HEALTH_REFRESH_MS / 1000} seconds while this page is visible.
          </p>
        </div>
      </header>

      {health.isPending ? (
        <div className="health-grid" aria-busy="true" aria-label="Reading the controller vitals">
          <div className="card health-card">
            <Skeleton className="h-8 w-full" />
            <Skeleton className="h-touch w-full" />
          </div>
          <div className="card health-card">
            <Skeleton className="h-8 w-full" />
            <Skeleton className="h-touch w-full" />
          </div>
        </div>
      ) : health.isError ? (
        <ErrorState
          title="Could not load the health report"
          detail="The controller did not answer. Nothing shown here is stale — there is simply nothing to show."
          status={health.error instanceof ApiError ? `${health.error.status} ${health.error.code}` : undefined}
          onRetry={() => void health.refetch()}
        />
      ) : health.data ? (
        <div className="health-grid">
          <HealthBody health={health.data} />
        </div>
      ) : (
        <EmptyState icon={HeartPulse} title="No health report" detail="The controller returned nothing to show." />
      )}
    </div>
  );
}
