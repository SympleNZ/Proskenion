/*
 * The health payload (spec §11.1, §11.2, §16.7 `GET /system/health`).
 *
 * Every value comes through the platform layer (§5.4), so a metric an
 * unsupported platform cannot read arrives as `null` with level `"unknown"`
 * and the screen reads "not available" rather than showing a wrong number.
 * The levels are the server's; the screen never recomputes a threshold.
 */
import type { Level } from "@/components/ui/levels";

export interface Cpu {
  temperature_c: number | null;
  level: Level;
}

export interface Memory {
  used_bytes: number | null;
  total_bytes: number | null;
  percent: number | null;
  level: Level;
}

export interface Storage {
  model: string | null;
  health: string | null;
  life_used_percent: number | null;
  temperature_c: number | null;
  media_errors: number | null;
  /** True when only part of the SMART data could be read (§11.1). */
  partial: boolean;
  level: Level;
}

export interface Partition {
  mount: string;
  /** A/B slot for the root partitions (§2.3); null for the data partitions. */
  slot: string | null;
  total_bytes: number | null;
  used_bytes: number | null;
  free_bytes: number | null;
  percent: number | null;
  level: Level;
}

export interface BackupMedia {
  present: boolean;
  absent_since: string | null;
  level: Level;
}

export interface BusHealth {
  drop_count_window: number | null;
  drop_consecutive_windows: number | null;
  unsubscribed: string[];
  level: Level;
}

export interface Application {
  loop_lag_p50_ms: number | null;
  loop_lag_p99_ms: number | null;
  level: Level;
  clients: number | null;
  bus: BusHealth;
}

export interface HealthDevice {
  key: string;
  name: string;
  category: string;
  status: "connected" | "degraded" | "error" | "unconfigured";
  kind: string | null;
  detail: string | null;
  last_seen: string | null;
  host: string | null;
  port: number | null;
  protocol: string | null;
  latency_ms: number | null;
  reconnects: number | null;
  last_error: string | null;
  level: Level;
}

export interface TimeHealth {
  synced: boolean;
  degraded: boolean;
  server_time: string | null;
}

export interface Health {
  platform: string;
  version: string;
  uptime_seconds: number | null;
  cpu: Cpu;
  memory: Memory;
  storage: Storage;
  partitions: Partition[];
  backup_media: BackupMedia;
  application: Application;
  devices: HealthDevice[];
  time: TimeHealth;
  overall: Level;
}
