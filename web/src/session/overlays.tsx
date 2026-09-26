/*
 * Session overlays (spec §6.5, §21.8). Over the current page, not a redirect:
 * the page behind is blurred but its state is preserved.
 */
import { CircleDashed, TriangleAlert } from "lucide-react";
import { useEffect, useId, useRef, useState, type FormEvent } from "react";

import { login, type LoginResponse } from "@/api/auth";
import { ApiError } from "@/api/client";
import { presentationFor } from "@/api/errors";
import { Button } from "@/components/ui/Button";
import { PasswordField } from "@/components/ui/Input";
import { useCountdown } from "@/components/useCountdown";
import { formatCountdown } from "@/lib/time";

export function ReauthOverlay({ onSignedIn }: { onSignedIn: (response: LoginResponse) => void }) {
  const titleId = useId();
  const descId = useId();
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const countdown = useCountdown();
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (busy || countdown.active || !password) return;
    setBusy(true);
    setError(null);
    try {
      const response = await login(password);
      onSignedIn(response);
    } catch (cause) {
      if (cause instanceof ApiError) {
        const p = presentationFor(cause);
        if (p.kind === "countdown") countdown.start(p.retryAfter);
        else if (cause.code === "unauthenticated" || cause.code === "permission_denied") setError("Incorrect password");
        else setError(p.message);
      } else {
        setError("Could not reach the controller");
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="session-backdrop" role="presentation">
      <form className="session-dialog" role="dialog" aria-modal="true" aria-labelledby={titleId} aria-describedby={descId} onSubmit={submit}>
        <h2 id={titleId}>Session expired</h2>
        <p id={descId}>
          Your session ended after a period of inactivity. Sign in again to continue. Your unsaved work is preserved.
        </p>
        <PasswordField
          ref={inputRef}
          label="Password"
          name="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          autoComplete="current-password"
          error={error}
          disabled={busy}
        />
        <Button type="submit" variant="primary" block loading={busy} disabled={countdown.active || !password}>
          {countdown.active ? `Sign in again — try again in ${formatCountdown(countdown.remaining)}` : "Sign in again"}
        </Button>
      </form>
    </div>
  );
}

export function AccessUpdatedOverlay({ onLeave }: { onLeave: () => void }) {
  const titleId = useId();
  const descId = useId();
  return (
    <div className="session-backdrop" role="presentation">
      <div className="session-dialog" role="alertdialog" aria-modal="true" aria-labelledby={titleId} aria-describedby={descId}>
        <h2 id={titleId}>
          <CircleDashed aria-hidden="true" className="text-fg-muted" />
          Access updated
        </h2>
        <p id={descId}>Your access has been updated by venue staff. Please speak to staff if you need to continue.</p>
        <Button type="button" variant="ghost" onClick={onLeave}>
          Back to the start
        </Button>
      </div>
    </div>
  );
}

/** Shared small warning glyph for inline errors (§21.8: "⚠ Incorrect password"). */
export function WarningGlyph() {
  return <TriangleAlert aria-hidden="true" className="size-4" />;
}
