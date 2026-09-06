/**
 * Auth state for the UI. Deliberately thin: the access token lives in
 * `api/client.ts`, in a module variable, and never enters React state —
 * React state ends up in devtools, error reports, and sometimes in
 * serialized error payloads. This context tracks only *whether* there is
 * a session, never the credential itself.
 */
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import {
  getClinicianName,
  getClinicianRole,
  login as apiLogin,
  logout as apiLogout,
  restoreSession,
  setSessionEndedHandler,
  stopProactiveRefresh,
} from "../api/client";

type AuthStatus = "checking" | "signed-in" | "signed-out";

/** A failed sign-in, with enough to render a live countdown when the
 * server named one (429 — rate-limited or locked-out) rather than just
 * a static "wait a few minutes" string that never updates. */
export type SignInFailure = { detail: string; retryAfterSeconds: number | null };

type AuthContextValue = {
  status: AuthStatus;
  /** A routing hint only — see `getClinicianRole`'s own comment. */
  role: string | null;
  /** Display only — see `getClinicianName`'s own comment. Whose account
   * this is, for the shared-clinic-laptop case; never a security check. */
  name: string | null;
  signIn: (email: string, password: string, mfaCode?: string) => Promise<SignInFailure | null>;
  signOut: () => Promise<void>;
};

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  // "checking" rather than "signed-out" on first paint: there may be a
  // valid httpOnly cookie, and flashing the login screen before we have
  // asked the server would be a lie shown to a doctor mid-shift.
  const [status, setStatus] = useState<AuthStatus>("checking");
  const [role, setRole] = useState<string | null>(null);
  const [name, setName] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    void restoreSession().then((ok) => {
      if (!cancelled) {
        setStatus(ok ? "signed-in" : "signed-out");
        setRole(ok ? getClinicianRole() : null);
        setName(ok ? getClinicianName() : null);
      }
    });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    // The client calls this when a refresh has definitively failed, which
    // is the only authoritative "your session is gone" signal.
    setSessionEndedHandler(() => {
      setStatus("signed-out");
      setRole(null);
      setName(null);
    });
    return () => setSessionEndedHandler(() => {});
  }, []);

  const signIn = useCallback(async (email: string, password: string, mfaCode?: string) => {
    const result = await apiLogin(email, password, mfaCode);
    if (result.ok) {
      setStatus("signed-in");
      setRole(getClinicianRole());
      setName(getClinicianName());
      return null;
    }
    return { detail: result.detail, retryAfterSeconds: result.retryAfterSeconds };
  }, []);

  const signOut = useCallback(async () => {
    await apiLogout();
    stopProactiveRefresh();
    setStatus("signed-out");
    setRole(null);
    setName(null);
  }, []);

  const value = useMemo(
    () => ({ status, role, name, signIn, signOut }),
    [status, role, name, signIn, signOut],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside <AuthProvider>");
  return ctx;
}
