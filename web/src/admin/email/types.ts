/* `/system/email*` wire types (phase-6-contracts.md §5, `proskenion/api/system.py`). */

export const TLS_MODES = ["none", "starttls", "tls"] as const;
export type TlsMode = (typeof TLS_MODES)[number];
export const DEFAULT_TLS_MODE: TlsMode = "starttls";

export const SMTP_FAILURE_STAGES = ["dns", "connect", "tls", "auth", "rejected_recipient"] as const;
export type SmtpFailureStage = (typeof SMTP_FAILURE_STAGES)[number];

/** `GET`/`PUT /system/email` — the password is write-only: `password_set`, never the value. */
export interface EmailConfig {
  host: string | null;
  port: number | null;
  tls_mode: TlsMode;
  username: string | null;
  password_set: boolean;
  sender: string | null;
  recipient: string | null;
  updated_at: string | null;
}

export interface EmailConfigUpdate {
  host: string;
  port: number;
  tls_mode: TlsMode;
  username: string | null;
  /** Omitted (not sent) keeps the stored password unchanged — never an explicit "clear" (§6.10). */
  password?: string;
  sender: string;
  recipient: string;
}

/** `POST /system/email/test` — every field optional: unset falls back to the saved row. */
export interface EmailTestRequest {
  host?: string;
  port?: number;
  tls_mode?: TlsMode;
  username?: string;
  password?: string;
  sender?: string;
  recipient?: string;
}

export interface EmailTestResult {
  ok: boolean;
  stage: SmtpFailureStage | null;
  message: string;
  /** Mirrored to smtp-fallback.toml for emergency mode (§4.6). `ok` with this false is a
   * delivered email whose mirror could not be saved; `message` says so. */
  fallback_saved: boolean;
}
