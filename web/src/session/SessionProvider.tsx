/*
 * Session state (spec §6.4, §6.5, §6.13, §21.8). Holds the tier and expiry
 * from GET /auth/session, runs the visibility hold, and decides what an
 * `unauthenticated` response means: the re-authentication overlay when the
 * session expired under 30 minutes ago, a full redirect to /login when
 * longer, and the "Access updated" overlay when a hirer has been revoked.
 */
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";

import { getSession, logout, type LoginResponse, type SessionResponse } from "@/api/auth";
import { ApiError, onApiEvent } from "@/api/client";
import { cachedSession, forgetLastKnown, rememberSession } from "@/offline/lastKnown";
import { enterOfflineStart, isUnreachable } from "@/offline/offlineSupport";

import { SessionContext, type Session, type SessionOverlay, type SessionStatus } from "./context";
import { decideUnauthenticated, parseTime, sessionFromResponse } from "./expiry";
import { AccessUpdatedOverlay, ReauthOverlay } from "./overlays";
import { useSessionHold } from "./useSessionHold";

export interface SessionProviderProps {
  children: ReactNode;
  /** Testing seam: skip the boot request and start from this state. */
  initial?: { status: SessionStatus; session: Session | null; serverTimeOffset?: number };
  holdIntervalMs?: number;
  /** How long the boot request may take before the controller counts as unreachable. */
  bootTimeoutMs?: number;
  /** How often a session drawn from the saved copy asks the controller again. */
  offlineRetryMs?: number;
}

/**
 * A reboot of the controller can leave its address answering nothing at all,
 * and a browser waits a long time on a connection that is never refused.
 */
export const BOOT_TIMEOUT_MS = 8_000;

/** Offline start: how often the saved session is checked against the controller. */
export const OFFLINE_RETRY_MS = 5_000;

export function SessionProvider({
  children,
  initial,
  holdIntervalMs,
  bootTimeoutMs = BOOT_TIMEOUT_MS,
  offlineRetryMs = OFFLINE_RETRY_MS,
}: SessionProviderProps) {
  const navigate = useNavigate();
  const [status, setStatus] = useState<SessionStatus>(initial?.status ?? "loading");
  const [session, setSession] = useState<Session | null>(initial?.session ?? null);
  const [serverTimeOffset, setServerTimeOffset] = useState(initial?.serverTimeOffset ?? 0);
  const [overlay, setOverlay] = useState<SessionOverlay>(null);
  // True while the session on screen is the saved copy from an offline start
  // (§21.27) and the controller has not yet confirmed it.
  const [fromCache, setFromCache] = useState(false);
  // Event handlers below read the latest session without re-subscribing.
  const sessionRef = useRef<Session | null>(session);
  useEffect(() => {
    sessionRef.current = session;
  }, [session]);

  const adopt = useCallback((response: SessionResponse) => {
    const parsed = sessionFromResponse(response);
    rememberSession(parsed.session);
    setSession(parsed.session);
    setServerTimeOffset(parsed.offset);
    setStatus("authenticated");
    setFromCache(false);
  }, []);

  const clear = useCallback(() => {
    forgetLastKnown();
    setSession(null);
    setStatus("anonymous");
    setOverlay(null);
    setFromCache(false);
  }, []);

  const refresh = useCallback(async () => {
    try {
      adopt(await getSession());
    } catch (error) {
      // unauthenticated and first-run are announced through apiEvents and
      // handled below; anything else (a blip) leaves the session as it was.
      if (!(error instanceof ApiError)) return;
    }
  }, [adopt]);

  const signIn = useCallback(
    (response: LoginResponse) => {
      // `certificate` is not on `LoginResponse` — the imminent `refresh()`
      // below fills it in from `GET /auth/session`, exactly as it fills in
      // `absoluteExpiresAt`. "Trusted" is the harmless default meanwhile: it
      // only ever makes the install prompt briefly consider offering itself,
      // never briefly hides §6.16's trust link from someone who needs it.
      const next: Session = {
        tier: response.tier,
        expiresAt: parseTime(response.expires_at) ?? Date.now(),
        absoluteExpiresAt: null,
        certificate: "trusted",
      };
      rememberSession(next);
      setSession(next);
      setStatus("authenticated");
      setFromCache(false);
      setOverlay(null);
      // Pick up absolute expiry and the appliance clock.
      void refresh();
    },
    [refresh],
  );

  const signOut = useCallback(async () => {
    try {
      await logout();
    } finally {
      clear();
      navigate("/login", { replace: true });
    }
  }, [clear, navigate]);

  // Boot: who is signed in, if anyone.
  useEffect(() => {
    if (initial) return undefined;
    const controller = new AbortController();
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, bootTimeoutMs);
    getSession({ quiet: true, signal: controller.signal })
      .then(adopt)
      .catch((error: unknown) => {
        if (controller.signal.aborted && !timedOut) return;
        if (error instanceof ApiError && error.code === "permission_denied" && error.reason === "first_run_incomplete") {
          setStatus("anonymous");
          navigate("/setup", { replace: true });
          return;
        }
        // The controller could not be asked (§21.27). With a saved session,
        // draw its surface offline, with cached values, rather than sending
        // a wall-mounted screen to the login page because a reboot was in
        // progress; the controller has the final word once it answers.
        const saved = timedOut || isUnreachable(error) ? cachedSession() : null;
        if (saved) {
          enterOfflineStart(saved.tier);
          setSession(saved);
          setStatus("authenticated");
          setFromCache(true);
          return;
        }
        setStatus("anonymous");
      })
      .finally(() => clearTimeout(timer));
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [initial, adopt, navigate, bootTimeoutMs]);

  // A saved session is only a stand-in: keep asking until the controller
  // answers, and let its answer decide — `adopt` on success, the usual
  // unauthenticated handling (overlay or login) if the session has ended.
  useEffect(() => {
    if (!fromCache || overlay !== null) return undefined;
    const interval = setInterval(() => void refresh(), offlineRetryMs);
    return () => clearInterval(interval);
  }, [fromCache, overlay, refresh, offlineRetryMs]);

  // Cross-cutting outcomes from any request.
  useEffect(() => {
    const offFirstRun = onApiEvent("first-run", () => {
      navigate("/setup", { replace: true });
    });
    const offUnauth = onApiEvent("unauthenticated", (error) => {
      const decision = decideUnauthenticated(sessionRef.current, error.reason);
      if (decision === "redirect") {
        clear();
        navigate("/login", { replace: true });
      } else {
        setOverlay(decision);
      }
    });
    return () => {
      offFirstRun();
      offUnauth();
    };
  }, [navigate, clear]);

  useSessionHold(status === "authenticated" && overlay === null, refresh, holdIntervalMs);

  const value = useMemo(
    () => ({ status, session, serverTimeOffset, overlay, signIn, refresh, signOut }),
    [status, session, serverTimeOffset, overlay, signIn, refresh, signOut],
  );

  return (
    <SessionContext.Provider value={value}>
      {children}
      {overlay === "expired" && <ReauthOverlay onSignedIn={signIn} />}
      {overlay === "revoked" && (
        <AccessUpdatedOverlay
          onLeave={() => {
            clear();
            navigate("/login", { replace: true });
          }}
        />
      )}
    </SessionContext.Provider>
  );
}
