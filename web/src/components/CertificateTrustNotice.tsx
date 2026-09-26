/*
 * The §21.8 self-signed notice (§6.16, a Phase 5 carry-forward): wherever a
 * self-signed certificate is in play, offer the
 * download and the exact trust steps, rather than describing the problem
 * with nothing to do about it. Shared between the Certificates screen
 * (`admin/certs/CertificatesScreen.tsx`) and the hirer install prompt
 * (`pwa/InstallPromptCard.tsx`), which see the same certificate from two
 * different tiers.
 *
 * `GET /system/certs/download` is deliberately unauthenticated (§6.16,
 * `api/certs.py`'s module docstring) — a device deciding whether to trust
 * this controller cannot first sign in to it — so this link works from the
 * login page, the install prompt and here alike.
 */
import { API_BASE } from "@/api/client";

export const CERT_DOWNLOAD_PATH = `${API_BASE}/system/certs/download`;

export function CertificateTrustNotice() {
  return (
    <>
      <a href={CERT_DOWNLOAD_PATH} download className="text-teal-300 underline underline-offset-2">
        Download the certificate
      </a>
      , then on the device: <strong>Settings → General → VPN &amp; Device Management → trust it</strong> (§6.16).
    </>
  );
}
