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
import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { useAuth } from "../lib/auth";
import { FieldError } from "../components/Banner";

export function Login() {
  const { signIn } = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    const detail = await signIn(email, password);
    setBusy(false);
    if (detail) setError(detail);
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

        <label htmlFor="password">Password</label>
        <input
          id="password"
          type="password"
          autoComplete="current-password"
          required
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />

        <FieldError>{error}</FieldError>

        <button type="submit" disabled={busy}>
          {busy ? "Signing in…" : "Sign in"}
        </button>

        <p className="muted" style={{ marginTop: "1rem" }}>
          No account yet? <Link to="/sign-up">Sign up</Link>.
        </p>
      </form>
    </main>
  );
}
