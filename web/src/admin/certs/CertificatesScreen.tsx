/*
 * Admin → Certificates (spec §21.24 *Certificates*, §6.16, §3.2, contracts
 * §5–§6, wave 3 additions). The card, the Cloudflare token (write-only),
 * renewal history, issuance through `ProgressPanel`'s six named steps, and
 * "Use self-signed" — the way back when issuance cannot work at all, which
 * reaches neither Cloudflare nor ACME (`POST /system/certs/self-signed`).
 */
import { useState } from "react";
import { Check, Minus, ShieldCheck, X } from "lucide-react";

import { ApiError } from "@/api/client";
import { presentError } from "@/api/errors";
import { CERT_DOWNLOAD_PATH, CertificateTrustNotice } from "@/components/CertificateTrustNotice";
import { ProgressPanel } from "@/components/ProgressPanel";
import { Banner } from "@/components/ui/Banner";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { EmptyState, ErrorState, Skeleton } from "@/components/ui/EmptyState";
import { Field, Input } from "@/components/ui/Input";
import { LevelDot } from "@/components/ui/LevelDot";
import { LEVEL_WORDS } from "@/components/ui/levels";
import { ConfirmDialog } from "@/components/ui/Sheet";
import { useProgress } from "@/live/store";
import { saveOnShortcut } from "@/lib/keyboard";

import { useCertificateHistory, useIssueCertificate, useSetToken, useTestToken, useTokenState, useUseSelfSigned } from "./api";
import { certificateLevel } from "./certLevel";
import { CERT_ISSUE_OPERATION, CERT_PROGRESS_STEPS, type RenewalRecord } from "./types";

function statusLine(error: unknown): string | undefined {
  return error instanceof ApiError ? `${error.status} ${error.code}` : undefined;
}

function formatDate(iso: string): string {
  const at = Date.parse(iso);
  if (Number.isNaN(at)) return iso;
  return new Date(at).toLocaleDateString("en-NZ", { day: "numeric", month: "long", year: "numeric" });
}

function ResultWord({ result }: { result: RenewalRecord["result"] }) {
  if (result === "success") {
    return (
      <span className="inline-flex items-center gap-2 text-success-text">
        <Check aria-hidden="true" strokeWidth={3} className="size-4" />
        Success
      </span>
    );
  }
  if (result === "skipped") {
    return (
      <span className="inline-flex items-center gap-2 text-fg-muted">
        <Minus aria-hidden="true" strokeWidth={3} className="size-4" />
        Skipped
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-2 text-danger-text">
      <X aria-hidden="true" strokeWidth={3} className="size-4" />
      Failed
    </span>
  );
}

export function CertificatesScreen() {
  const history = useCertificateHistory();
  const tokenState = useTokenState();
  const setToken = useSetToken();
  const testToken = useTestToken();
  const issue = useIssueCertificate();
  const selfSigned = useUseSelfSigned();
  // Live, not just this tab's own mutation: a weekly automatic renewal, or
  // another admin's Renew, streams the same `cert_issue`/`cert_renew`
  // progress over the socket (§21.24 does not say "only the tab that
  // started it") — `progress.step < progress.of` is what stops this staying
  // "true" forever once a run has finished.
  const liveProgress = useProgress(CERT_ISSUE_OPERATION);
  const issuing = issue.isPending || (liveProgress !== null && liveProgress.step < liveProgress.of);

  const [editingToken, setEditingToken] = useState(false);
  const [tokenValue, setTokenValue] = useState("");
  const [tokenError, setTokenError] = useState<string | undefined>();
  const [testResult, setTestResult] = useState<{ ok: boolean; message: string } | null>(null);
  const [issueError, setIssueError] = useState<string | undefined>();
  const [selfSignedConfirmOpen, setSelfSignedConfirmOpen] = useState(false);
  const [selfSignedError, setSelfSignedError] = useState<string | undefined>();

  if (history.isPending || tokenState.isPending) {
    return (
      <div className="view certs-view" aria-busy="true" aria-label="Loading the certificate configuration">
        <h1 className="view-title">Certificates</h1>
        <Skeleton className="h-touch w-full" />
        <Skeleton className="h-touch w-full" />
      </div>
    );
  }

  if (history.isError || tokenState.isError) {
    return (
      <div className="view certs-view">
        <h1 className="view-title">Certificates</h1>
        <ErrorState
          title="Could not load the certificate configuration"
          status={statusLine(history.error ?? tokenState.error)}
          onRetry={() => {
            void history.refetch();
            void tokenState.refetch();
          }}
        />
      </div>
    );
  }

  const card = history.data?.certificate ?? null;
  const records = history.data?.history ?? [];
  const level = certificateLevel(card);
  const configured = tokenState.data?.configured ?? false;

  function saveToken(): void {
    setTokenError(undefined);
    if (!tokenValue.trim()) {
      setTokenError("Enter the API token.");
      return;
    }
    setToken.mutate(tokenValue, {
      onSuccess: () => {
        setEditingToken(false);
        setTokenValue("");
      },
      onError: (error) => {
        if (error instanceof ApiError && error.code === "validation_failed") {
          setTokenError(error.message);
          return;
        }
        presentError(error);
      },
    });
  }

  function runTest(): void {
    setTestResult(null);
    testToken.mutate(undefined, {
      onSuccess: (result) => setTestResult({ ok: result.ok, message: result.ok ? "Token verified — zone access confirmed" : "Token did not verify" }),
      onError: (error) => setTestResult({ ok: false, message: error instanceof ApiError ? error.message : "Could not test the token" }),
    });
  }

  function renew(): void {
    setIssueError(undefined);
    issue.mutate(null, {
      onError: (error) => setIssueError(error instanceof ApiError ? error.message : "Could not issue the certificate"),
    });
  }

  function doUseSelfSigned(): void {
    setSelfSignedConfirmOpen(false);
    setSelfSignedError(undefined);
    selfSigned.mutate(null, {
      onError: (error) =>
        setSelfSignedError(error instanceof ApiError ? error.message : "Could not generate a self-signed certificate"),
    });
  }

  return (
    <div className="view certs-view">
      <header className="view-head">
        <div>
          <h1 className="view-title">Certificates</h1>
          <p className="view-lede">The TLS certificate this controller serves, renewed weekly through Let&apos;s Encrypt.</p>
        </div>
      </header>

      <Card className="device-card" title="TLS certificate" titleLevel="h2">
        {card ? (
          <>
            <span className="level-badge" data-level={level}>
              <LevelDot level={level} subject="Certificate" className={card.expired ? "status-dot-pulse" : ""} />
              <span>{card.expired ? "Expired" : LEVEL_WORDS[level]}</span>
            </span>
            <dl className="kv">
              <dt>Domain</dt>
              <dd>{card.domain}</dd>
              <dt>Issuer</dt>
              <dd>{card.issuer}</dd>
              <dt>Issued</dt>
              <dd>{formatDate(card.issued)}</dd>
              <dt>Expires</dt>
              <dd>
                {formatDate(card.expires)} · {card.expired ? "expired" : `${card.days_remaining} days`}
              </dd>
              <dt>Auto-renew</dt>
              <dd>{card.self_signed ? "Not while self-signed" : "Weekly check"}</dd>
            </dl>

            {card.self_signed ? (
              <Banner tone="warning" title="Using a self-signed certificate">
                <p>
                  <CertificateTrustNotice />
                </p>
              </Banner>
            ) : null}
          </>
        ) : (
          <EmptyState icon={ShieldCheck} title="No certificate installed" detail="Issue one below, or check back once DNS is configured." />
        )}

        {issuing ? (
          <ProgressPanel operation={CERT_ISSUE_OPERATION} steps={CERT_PROGRESS_STEPS} label="Certificate issuance progress" />
        ) : null}
        {issueError ? (
          <p className="field-note" role="alert">
            {issueError}
          </p>
        ) : null}
        {selfSignedError ? (
          <p className="field-note" role="alert">
            {selfSignedError}
          </p>
        ) : null}

        <div className="flex gap-3">
          <Button variant="primary" helpId="certs.renew" loading={issuing} onClick={renew}>
            Renew now
          </Button>
          <a className="btn btn-secondary" href={CERT_DOWNLOAD_PATH} download>
            View certificate
          </a>
          <Button
            variant="secondary"
            loading={selfSigned.isPending}
            onClick={() => setSelfSignedConfirmOpen(true)}
          >
            Use self-signed
          </Button>
        </div>
      </Card>

      <ConfirmDialog
        open={selfSignedConfirmOpen}
        onOpenChange={setSelfSignedConfirmOpen}
        title="Switch to a self-signed certificate?"
        description={
          <>
            This is the way back when Let&apos;s Encrypt cannot work — an expired token, no DNS, a site off the
            internet. It reaches neither Cloudflare nor ACME. Every browser will warn that this controller&apos;s
            certificate is not trusted, and an iPad will refuse the live connection outright until it is told to
            trust it (§6.16) — the certificate can still be downloaded and trusted from here, or from the login page.
          </>
        }
        confirmLabel="Use self-signed"
        destructive
        onConfirm={doUseSelfSigned}
      />

      <Card className="device-card" title="Cloudflare DNS" titleLevel="h2">
        <Field label="API token" htmlFor="certs-token" helpId="certs.token" error={tokenError} errorId="certs-token-error">
          {editingToken ? (
            <div className="flex gap-2" onKeyDown={saveOnShortcut(saveToken)}>
              <Input
                id="certs-token"
                type="password"
                autoComplete="off"
                value={tokenValue}
                onChange={(e) => setTokenValue(e.currentTarget.value)}
              />
              <Button variant="primary" helpId="certs.token-save" loading={setToken.isPending} onClick={saveToken}>
                Save
              </Button>
              <Button
                variant="ghost"
                onClick={() => {
                  setEditingToken(false);
                  setTokenValue("");
                  setTokenError(undefined);
                }}
              >
                Cancel
              </Button>
            </div>
          ) : (
            <div className="flex items-center gap-3">
              <span className="technical" aria-hidden="true">
                {configured ? "●●●●●●●●●●" : "Not set"}
              </span>
              <span className="sr-only">{configured ? "A token is set" : "No token is set"}</span>
              <Button variant="secondary" onClick={() => setEditingToken(true)}>
                {configured ? "Change" : "Set"}
              </Button>
              <Button variant="secondary" loading={testToken.isPending} disabled={!configured} onClick={runTest}>
                Test
              </Button>
            </div>
          )}
        </Field>
        {testResult ? (
          <p className={testResult.ok ? "text-success-text" : "text-danger-text"} role="status">
            {testResult.message}
          </p>
        ) : null}
        <p className="field-help">
          Scoped to one zone with DNS edit permission only (§3.2). The stored value is never displayed — only whether
          one is set.
        </p>
      </Card>

      <Card className="device-card" title="Renewal history" titleLevel="h2">
        {records.length === 0 ? (
          <p className="text-fg-muted text-sm">No renewal attempts recorded yet.</p>
        ) : (
          <div className="table-scroll">
            <table className="data-table">
              <caption className="sr-only">Certificate renewal history</caption>
              <thead>
                <tr>
                  <th scope="col">Date</th>
                  <th scope="col">Method</th>
                  <th scope="col">Result</th>
                  <th scope="col">Detail</th>
                </tr>
              </thead>
              <tbody>
                {records.map((record) => (
                  <tr key={`${record.attempted_at}-${record.method}`}>
                    <td>{formatDate(record.attempted_at)}</td>
                    <td className="capitalize">{record.method}</td>
                    <td>
                      <ResultWord result={record.result} />
                    </td>
                    <td className="text-fg-muted text-sm">{record.detail ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}
