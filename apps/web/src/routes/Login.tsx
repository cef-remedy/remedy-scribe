/**
 * Login: email + password.
 *
 * ⚠️ MFA is currently off, deployment-wide — `settings.require_mfa=False`
 * on the API (`app/core/config.py`), a demo-stage toggle, not a deleted
 * capability. This form simply never sends `mfa_code` (optional on the
 * API side too), which is why there is no field for it here at all. If
 * `require_mfa` is ever turned back on for a real pilot, this form needs
 * the field restored — `git log` on this file has the version that had
 * it, including the local-dev auto-fill in `lib/totp.ts`.
 */
import { useEffect, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { useAuth } from "../lib/auth";
import { FieldError } from "../components/Banner";
import { PasswordField } from "../components/PasswordField";

/** "1:05" / "0:09" — never bare seconds, so it reads as a countdown and
 * not an ambiguous number. */
function formatCountdown(ms: number): string {
  const totalSeconds = Math.ceil(ms / 1000);
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${minutes}:${String(seconds).padStart(2, "0")}`;
}

export function Login() {
  const { signIn } = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // The wall-clock instant the lockout/rate-limit the server named
  // actually clears, not a seconds count that goes stale the moment a
  // render is skipped — ticking against a fixed instant self-corrects
  // for that instead of drifting.
  const [lockedUntil, setLockedUntil] = useState<number | null>(null);
  const [remainingMs, setRemainingMs] = useState(0);

  useEffect(() => {
    if (lockedUntil === null) return undefined;
    const tick = () => setRemainingMs(Math.max(0, lockedUntil - Date.now()));
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, [lockedUntil]);

  // Once the countdown reaches zero, stop showing a locked-out state —
  // the server is the actual authority on whether it's clear, but there
  // is no reason to keep the button disabled and a stale error on screen
  // past the moment we already know it's expired.
  useEffect(() => {
    if (lockedUntil !== null && remainingMs === 0) {
      setLockedUntil(null);
      setError(null);
    }
  }, [remainingMs, lockedUntil]);

  const locked = lockedUntil !== null && remainingMs > 0;

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    const failure = await signIn(email, password);
    setBusy(false);
    if (failure) {
      setError(failure.detail);
      setLockedUntil(
        failure.retryAfterSeconds != null ? Date.now() + failure.retryAfterSeconds * 1000 : null,
      );
    }
  }

  return (
    <main className="auth">
      <form className="card" onSubmit={onSubmit}>
        <h1>Remedy Scribe</h1>
        <p className="muted">Sign in to start documenting consultations.</p>

        <label htmlFor="email">Email</label>
        <input
          id="email"
          type="email"
          autoComplete="username"
          required
          value={email}
          onChange={(e) => setEmail(e.target.value)}
        />

        <PasswordField
          id="password"
          label="Password"
          autoComplete="current-password"
          required
          value={password}
          onChange={setPassword}
        />

        <FieldError>{error}</FieldError>

        <button type="submit" disabled={busy || locked}>
          {locked
            ? `Try again in ${formatCountdown(remainingMs)}`
            : busy
              ? "Signing in…"
              : "Sign in"}
        </button>

        <p className="muted" style={{ marginTop: "1rem" }}>
          No account yet? <Link to="/sign-up">Sign up</Link>.
        </p>
      </form>
    </main>
  );
}
