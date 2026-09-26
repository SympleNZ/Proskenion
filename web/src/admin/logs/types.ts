/* `GET/PUT /system/security-log` and `/system/debug-logging` (spec §6.14, §4.10, §21.24 "Logs"). */

export const SECURITY_EVENT_TYPES = [
  "login_success",
  "login_failure",
  "lockout",
  "permission_denied",
  "config_changed",
  "pin_changed",
  "access_toggled",
  "password_changed",
  "forced_logout",
  "unexpected_origin",
  "update_applied",
  "update_auto_rollback",
  "baseline_restored",
  "certificate_renewed",
  "backup_restored",
  "os_upgrade_applied",
  "os_upgrade_rolled_back",
] as const;

export type SecurityEventType = (typeof SECURITY_EVENT_TYPES)[number];

export type SecurityOutcome = "success" | "failure";

export interface SecurityLogEntry {
  id: number;
  timestamp: string;
  event_type: string;
  outcome: SecurityOutcome;
  user_ident: string | null;
  ip_address: string | null;
  detail: Record<string, unknown> | null;
}

export interface SecurityLogResponse {
  entries: readonly SecurityLogEntry[];
}

export interface SecurityLogFilter {
  eventType?: string | undefined;
  outcome?: SecurityOutcome | undefined;
  ipAddress?: string | undefined;
  from?: string | undefined;
  to?: string | undefined;
  limit: number;
  offset: number;
}

export const SYSTEM_LOG_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] as const;

export type SystemLogLevel = (typeof SYSTEM_LOG_LEVELS)[number];

export interface SystemLogEntry {
  timestamp: string;
  level: string;
  logger: string;
  message: string;
  context: Record<string, unknown>;
}

export interface SystemLogResponse {
  entries: readonly SystemLogEntry[];
  has_more: boolean;
}

export interface SystemLogFilter {
  level?: SystemLogLevel | undefined;
  module?: string | undefined;
  from?: string | undefined;
  to?: string | undefined;
  limit: number;
  offset: number;
}

export interface DebugLoggerState {
  name: string;
  enabled: boolean;
}

export interface DebugLoggingResponse {
  loggers: readonly DebugLoggerState[];
}

export interface DebugLoggingUpdate {
  logger: string;
  enabled: boolean;
}
