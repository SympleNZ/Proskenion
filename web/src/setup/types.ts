/*
 * First-run wizard contract (spec §10.4, §16.4).
 *
 * The wizard is the only bootstrap path: the appliance must be fully
 * configurable from it with no command line. All three endpoints are public,
 * because there is no credential to present until step 2 has run, and all
 * three refuse with `permission_denied` and `detail.reason = "setup_complete"`
 * once step 7 commits.
 */

export const STEP_COUNT = 7;

export const MIN_PASSWORD_LENGTH = 12;

export type StepKey =
  | "welcome"
  | "admin_password"
  | "network"
  | "devices"
  | "operator_password"
  | "certificate"
  | "summary";

export interface SetupStep {
  step: number;
  key: StepKey;
  label: string;
  completed: boolean;
  completed_at: string | null;
  /** Safe to display: never a password, never any secret. */
  summary: Record<string, unknown>;
}

/** Locale, timezone, platform, address and hostname inherited from the OS. */
export interface Detected {
  locale: string;
  timezone: string;
  platform: string;
  hostname: string;
  address: string | null;
}

export interface CertificateOption {
  id: string;
  label: string;
  available: boolean;
  /** Why an unavailable option cannot be chosen — named, never merely greyed out. */
  reason: string | null;
  guidance: string[];
}

export interface InstalledCertificate {
  domain: string;
  issuer: string;
  issued: string | null;
  expires: string | null;
  days_remaining: number | null;
  self_signed: boolean;
  renewal_history: unknown[];
}

export interface CertificateState {
  hostname: string;
  options: CertificateOption[];
  installed: InstalledCertificate | null;
}

export interface SetupState {
  first_run: boolean;
  steps: SetupStep[];
  /** The first incomplete step: where an abandoned setup resumes. */
  next_step: number | null;
  detected: Detected;
  certificate: CertificateState;
}

export interface StepResponse {
  step: SetupStep;
  next_step: number | null;
  steps: SetupStep[];
  /** Set only by step 2: it signs the wizard in the same way `POST /auth/login` does. */
  tier?: string;
  expires_at?: string;
  /**
   * Set only by step 6: whether the certificate nginx serves was replaced.
   * When it was, a browser that accepted the old self-signed certificate
   * refuses the next request at TLS, so the user is told before it.
   */
  certificate_replaced?: boolean;
  /** Step 6: the names and addresses the served certificate is valid for. */
  certificate_names?: string[];
}

export interface CompleteResponse {
  first_run: false;
  completed_at: string | null;
  steps: SetupStep[];
}
