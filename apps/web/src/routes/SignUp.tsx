/**
 * Self-service sign-up — did not exist before the free-tier demo. Every
 * account before this came from the seed script.
 *
 * Always creates a `doctor` — there is no role field on this form because
 * there is none on the API request either (`RegisterRequest`'s own
 * docstring: letting a signup form choose its own role would let anyone
 * grant themselves `admin` or `compliance`, both RBAC-gated). Appropriate
 * for this demo/pre-pilot stage, not a real clinic — nothing here verifies
 * the email or asks for a PRC license.
 */
import { useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { register } from "../api/client";
import { useAuth } from "../lib/auth";
import { FieldError } from "../components/Banner";

export function SignUp() {
  const { signIn } = useAuth();
  const navigate = useNavigate();
  const [fullName, setFullName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    setError(null);

    if (password !== confirm) {
      setError("Those two passwords don't match.");
      return;
    }
    if (password.length < 8) {
      setError("Password must be at least 8 characters.");
      return;
    }

    setBusy(true);
    const result = await register(email, password, fullName);
    if (!result.ok) {
      setBusy(false);
      setError(result.detail);
      return;
    }

    // Straight into the app — the account just proved it's real by
    // choosing its own password, and asking for it again immediately
    // would only be friction, not security.
    const loginDetail = await signIn(email, password);
    setBusy(false);
    if (loginDetail) {
      // Created but couldn't sign in automatically — rare, but send them
      // to the normal sign-in screen rather than leaving this one stuck.
      navigate("/login");
      return;
    }
    navigate("/");
  }

  return (
    <main className="auth">
      <form className="card" onSubmit={onSubmit}>
        <h1>Create your account</h1>
        <p className="muted">For documenting your own consultations at Remedy.</p>

        <label htmlFor="full-name">Full name</label>
        <input
          id="full-name"
          type="text"
          autoComplete="name"
          required
          placeholder="Dr. Maria Santos"
          value={fullName}
          onChange={(e) => setFullName(e.target.value)}
        />

        <label htmlFor="signup-email">Email</label>
        <input
          id="signup-email"
          type="email"
          autoComplete="username"
          required
          value={email}
          onChange={(e) => setEmail(e.target.value)}
        />

        <label htmlFor="signup-password">Password</label>
        <input
          id="signup-password"
          type="password"
          autoComplete="new-password"
          required
          minLength={8}
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />

        <label htmlFor="signup-confirm">Confirm password</label>
        <input
          id="signup-confirm"
          type="password"
          autoComplete="new-password"
          required
          value={confirm}
          onChange={(e) => setConfirm(e.target.value)}
        />

        <FieldError>{error}</FieldError>

        <button type="submit" disabled={busy}>
          {busy ? "Creating account…" : "Create account"}
        </button>

        <p className="muted" style={{ marginTop: "1rem" }}>
          Already have an account? <Link to="/login">Sign in</Link>.
        </p>
      </form>
    </main>
  );
}
