/*
 * Login (spec §21.8). One page at /login, staff above and hirer below. One
 * password field, no username. Six PIN boxes. Errors exactly as specified:
 * a wrong password keeps the field value; a wrong PIN shakes then clears and
 * leaves the staff field alone; rate limiting replaces the button label with
 * a live countdown and re-enables at zero.
 *
 * The build version shown discreetly below both forms (Simon's request)
 * discloses nothing `/health` does not already answer unauthenticated —
 * it just saves a school IT contact one extra request when reading it out
 * over the phone.
 */
import { useEffect, useId, useRef, useState, type FormEvent } from "react";
import { Navigate, useNavigate } from "react-router-dom";

import { hirerLogin, homeFor, login, type LoginResponse } from "@/api/auth";
import { ApiError } from "@/api/client";
import { presentError, presentationFor } from "@/api/errors";
import { PinInput } from "@/components/PinInput";
import { Button } from "@/components/ui/Button";
import { PasswordField } from "@/components/ui/Input";
import { useCountdown } from "@/components/useCountdown";
import { formatCountdown } from "@/lib/time";
import { useSession } from "@/session/context";
import { BUILD_VERSION } from "@/version/buildVersion";

export const HIRER_DISABLED_MESSAGE = "Hire guest access is not currently available. Please contact venue staff.";

/** How long the success glow and the shake are given before moving on (matches --duration-slow). */
const SETTLE_MS = 400;

export function LoginPage() {
  const navigate = useNavigate();
  const { status, session, signIn } = useSession();

  const [password, setPassword] = useState("");
  const [staffError, setStaffError] = useState<string | null>(null);
  const [staffBusy, setStaffBusy] = useState(false);
  const [staffSuccess, setStaffSuccess] = useState(false);
  const staffCountdown = useCountdown();

  const [pin, setPin] = useState("");
  const [pinError, setPinError] = useState<string | null>(null);
  const [pinBusy, setPinBusy] = useState(false);
  const [pinGlow, setPinGlow] = useState(false);
  const [pinShake, setPinShake] = useState(false);
  const [hirerDisabled, setHirerDisabled] = useState(false);
  const pinCountdown = useCountdown();
  const pinErrorId = useId();
  const pinHeadingId = useId();

  const timers = useRef<number[]>([]);
  useEffect(() => {
    const pending = timers.current;
    return () => pending.forEach((t) => window.clearTimeout(t));
  }, []);
  const later = (fn: () => void, ms: number) => {
    timers.current.push(window.setTimeout(fn, ms));
  };

  const finish = (response: LoginResponse) => {
    signIn(response);
    navigate(homeFor(response.tier), { replace: true });
  };

  const submitStaff = async (event: FormEvent) => {
    event.preventDefault();
    if (staffBusy || staffCountdown.active || !password) return;
    setStaffBusy(true);
    setStaffError(null);
    try {
      const response = await login(password);
      setStaffSuccess(true);
      later(() => finish(response), SETTLE_MS);
    } catch (cause) {
      if (cause instanceof ApiError) {
        const p = presentationFor(cause);
        if (p.kind === "countdown") staffCountdown.start(p.retryAfter);
        else if (cause.code === "unauthenticated" || cause.code === "permission_denied") setStaffError("Incorrect password");
        else if (p.kind === "inline") setStaffError(p.fields["password"] ?? p.message);
        else setStaffError(presentError(cause)?.message ?? cause.message);
      } else {
        setStaffError("Could not reach the controller");
      }
    } finally {
      setStaffBusy(false);
    }
  };

  const submitPin = async (value: string) => {
    if (pinBusy || pinCountdown.active || value.length !== 6) return;
    setPinBusy(true);
    setPinError(null);
    setPinGlow(true);
    try {
      const response = await hirerLogin(value);
      later(() => finish(response), SETTLE_MS);
    } catch (cause) {
      setPinGlow(false);
      if (cause instanceof ApiError) {
        const p = presentationFor(cause);
        if (cause.code === "permission_denied" && cause.reason === "hirer_disabled") {
          setHirerDisabled(true);
        } else if (p.kind === "countdown") {
          pinCountdown.start(p.retryAfter);
        } else if (cause.code === "unauthenticated" || cause.code === "permission_denied") {
          setPinError("Incorrect PIN");
          setPinShake(true);
          later(() => {
            setPinShake(false);
            setPin("");
          }, SETTLE_MS);
        } else {
          setPinError(presentError(cause)?.message ?? cause.message);
        }
      } else {
        setPinError("Could not reach the controller");
      }
    } finally {
      setPinBusy(false);
    }
  };

  if (status === "authenticated" && session && !staffSuccess && !pinGlow) {
    return <Navigate to={homeFor(session.tier)} replace />;
  }

  return (
    <main className="login-page">
      <div className="login-brand">
        <h1>Proskenion</h1>
        <div className="brand-host">{window.location.hostname}</div>
      </div>

      <div className="login-column">
        <form className="card login-card" onSubmit={submitStaff} aria-label="Staff sign in" noValidate>
          <PasswordField
            label="Password"
            name="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password"
            autoFocus
            error={staffError}
            disabled={staffBusy}
          />
          <Button
            type="submit"
            variant="primary"
            block
            loading={staffBusy}
            success={staffSuccess}
            disabled={staffCountdown.active}
            data-testid="sign-in"
          >
            {staffCountdown.active ? `Sign in — try again in ${formatCountdown(staffCountdown.remaining)}` : "Sign in"}
          </Button>
        </form>

        <div className="divider" role="separator">
          or
        </div>

        <section className="flex flex-col gap-4" aria-labelledby={pinHeadingId}>
          <h2 id={pinHeadingId} className="section-title">
            Hire guest? Enter your PIN
          </h2>
          {hirerDisabled ? (
            <p className="note" role="status" aria-live="polite">
              {HIRER_DISABLED_MESSAGE}
            </p>
          ) : (
            <>
              <PinInput
                value={pin}
                onChange={setPin}
                onComplete={(value) => void submitPin(value)}
                disabled={pinBusy || pinCountdown.active}
                shaking={pinShake}
                glowing={pinGlow}
                invalid={pinError !== null}
                describedBy={pinErrorId}
              />
              <div className="field-error" id={pinErrorId} role="alert" aria-live="assertive">
                {pinError ? <span>⚠ {pinError}</span> : null}
              </div>
              <Button
                variant="secondary"
                size="primary"
                block
                loading={pinBusy}
                disabled={pinCountdown.active || pin.length !== 6}
                onClick={() => void submitPin(pin)}
                data-testid="guest-access"
              >
                {pinCountdown.active ? `Guest access — try again in ${formatCountdown(pinCountdown.remaining)}` : "Guest access"}
              </Button>
            </>
          )}
        </section>
      </div>

      <p className="login-footer technical text-fg-muted text-xs text-center">Proskenion v{BUILD_VERSION}</p>
    </main>
  );
}
