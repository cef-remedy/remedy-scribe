/**
 * Login: email + password + authenticator code, all in one step, because
 * that is what the API actually does (`POST /api/v1/auth/login` takes all
 * three and returns the same 401 whichever factor was wrong, so as not to
 * leak which one).
 */
import { useState, type FormEvent } from "react";
import { useAuth } from "../lib/auth";
import { FieldError } from "../components/Banner";

export function Login() {
  const { signIn } = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [mfaCode, setMfaCode] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    const detail = await signIn(email, password, mfaCode);
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

        <label htmlFor="mfa">Authenticator code</label>
        <input
          id="mfa"
          type="text"
          inputMode="numeric"
          autoComplete="one-time-code"
          pattern="[0-9]*"
          maxLength={6}
          required
          placeholder="123456"
          value={mfaCode}
          onChange={(e) => setMfaCode(e.target.value.replace(/\D/g, ""))}
        />

        <FieldError>{error}</FieldError>

        <button type="submit" disabled={busy}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </main>
  );
}
