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
  login as apiLogin,
  logout as apiLogout,
  restoreSession,
  setSessionEndedHandler,
  stopProactiveRefresh,
} from "../api/client";

type AuthStatus = "checking" | "signed-in" | "signed-out";

type AuthContextValue = {
  status: AuthStatus;
  signIn: (email: string, password: string, mfaCode: string) => Promise<string | null>;
  signOut: () => Promise<void>;
};

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  // "checking" rather than "signed-out" on first paint: there may be a
  // valid httpOnly cookie, and flashing the login screen before we have
  // asked the server would be a lie shown to a doctor mid-shift.
  const [status, setStatus] = useState<AuthStatus>("checking");

  useEffect(() => {
    let cancelled = false;
    void restoreSession().then((ok) => {
      if (!cancelled) setStatus(ok ? "signed-in" : "signed-out");
    });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    // The client calls this when a refresh has definitively failed, which
    // is the only authoritative "your session is gone" signal.
    setSessionEndedHandler(() => setStatus("signed-out"));
    return () => setSessionEndedHandler(() => {});
  }, []);

  const signIn = useCallback(async (email: string, password: string, mfaCode: string) => {
    const result = await apiLogin(email, password, mfaCode);
    if (result.ok) {
      setStatus("signed-in");
      return null;
    }
    return result.detail;
  }, []);

  const signOut = useCallback(async () => {
    await apiLogout();
    stopProactiveRefresh();
    setStatus("signed-out");
  }, []);

  const value = useMemo(() => ({ status, signIn, signOut }), [status, signIn, signOut]);
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside <AuthProvider>");
  return ctx;
}
