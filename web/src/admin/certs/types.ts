/* `/system/certs*` wire types (phase-6-contracts.md §5, `proskenion/api/certs.py`). */

export interface RenewalRecord {
  attempted_at: string;
  method: "manual" | "automatic";
  result: "success" | "failed" | "skipped";
  issuer: string | null;
  serial: string | null;
  detail: string | null;
}

export interface CertificateCard {
  domain: string;
  issuer: string;
  issued: string;
  expires: string;
  days_remaining: number;
  self_signed: boolean;
  expired: boolean;
  renewal_history: RenewalRecord[];
}

export interface CertificateCardResponse {
  certificate: CertificateCard | null;
}

export interface HistoryResponse {
  certificate: CertificateCard | null;
  history: RenewalRecord[];
}

export interface TokenStateResponse {
  configured: boolean;
}

export interface TokenTestResponse {
  ok: boolean;
}

/**
 * `core/certs.py`'s `PROGRESS_STEPS` (contracts §6 `cert_issue`/`cert_renew`,
 * §21.24: "requesting, creating the TXT record, waiting for propagation,
 * verifying, downloading, reloading nginx"). Copied, not derived — the
 * server owns the real list; a mismatch here would only be discovered when
 * a step's live label read wrong.
 */
export const CERT_PROGRESS_STEPS: readonly string[] = [
  "Requesting",
  "Creating the TXT record",
  "Waiting for propagation",
  "Verifying",
  "Downloading",
  "Reloading nginx",
];

/** The one operation name `POST /system/certs/issue` reports under (contracts §6). */
export const CERT_ISSUE_OPERATION = "cert_issue";
