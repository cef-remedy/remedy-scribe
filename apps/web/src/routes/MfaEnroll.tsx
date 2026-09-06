/**
 * MFA enrollment — did not exist before this redesign.
 *
 * `POST /auth/mfa/enroll` and `/mfa/enroll/confirm` have existed since
 * Phase 0.3, fully typed on this client, and never called from anywhere.
 * The only way any account ever got MFA set up was a seed script writing a
 * secret directly into the database — a real new clinician had no path of
 * their own.
 *
 * Deliberately **not** behind a login: both routes prove identity with
 * email + password themselves (see `mfa_enroll`'s docstring on the API
 * side), because a clinician with no MFA yet cannot log in at all — this
 * screen exists specifically for someone who cannot reach `/login` yet.
 * An already-enrolled account re-provisioning here is refused server-side
 * (409) on purpose: re-enrollment for an active account needs an
 * admin/support path, not a self-service one (decision 0007).
 */
import { useCallback, useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import QRCode from "qrcode";
import { api } from "../api/client";
import { FieldError } from "../components/Banner";
import { PasswordField } from "../components/PasswordField";

type Step = "identify" | "scan" | "done";

export function MfaEnroll() {
  const navigate = useNavigate();
  const [step, setStep] = useState<Step>("identify");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [secretUri, setSecretUri] = useState<string | null>(null);
  const [qrDataUrl, setQrDataUrl] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const requestSecret = useCallback(
    async (event: FormEvent) => {
      event.preventDefault();
      setBusy(true);
      setError(null);
      try {
        const { data, error: apiError, response } = await api.POST("/api/v1/auth/mfa/enroll", {
          body: { email, password },
        });
        if (apiError || !data) {
          setError(
            response.status === 409
              ? "MFA is already set up for this account. If you've lost your authenticator, ask an admin — re-enrollment isn't self-service."
              : "Could not start enrollment — check the email and password.",
          );
          return;
        }
        setSecretUri(data.provisioning_uri);
        setQrDataUrl(await QRCode.toDataURL(data.provisioning_uri, { margin: 1, width: 220 }));
        setStep("scan");
      } catch {
        setError("Could not reach the server. Check your connection and try again.");
      } finally {
        setBusy(false);
      }
    },
    [email, password],
  );

  const confirmCode = useCallback(
    async (event: FormEvent) => {
      event.preventDefault();
      setBusy(true);
      setError(null);
      try {
        const { data, error: apiError, response } = await api.POST("/api/v1/auth/mfa/enroll/confirm", {
          body: { email, password, code },
        });
        if (apiError || !data?.enrolled) {
          setError(
            response.status === 401
              ? "That code didn't match. Codes expire every 30 seconds — try the next one your app shows."
              : "Could not confirm enrollment. Try again.",
          );
          return;
        }
        setStep("done");
      } catch {
        setError("Could not reach the server. Check your connection and try again.");
      } finally {
        setBusy(false);
      }
    },
    [email, password, code],
  );

  if (step === "done") {
    return (
      <main className="auth">
        <div className="card">
          <h1>Authenticator set up</h1>
          <p className="muted">
            MFA is now active on this account. Sign in with your password and the code your
            authenticator app shows.
          </p>
          <button type="button" onClick={() => navigate("/login")}>
            Go to sign in
          </button>
        </div>
      </main>
    );
  }

  if (step === "scan") {
    return (
      <main className="auth">
        <form className="card" onSubmit={(e) => void confirmCode(e)}>
          <h1>Scan this code</h1>
          <p className="muted">
            Open your authenticator app (Google Authenticator, Authy, or similar) and scan the code
            below. It won't take effect until you confirm it with a code on the next line.
          </p>

          {qrDataUrl && (
            <div className="qr-wrap">
              <img src={qrDataUrl} alt="Authenticator QR code" width={220} height={220} />
            </div>
          )}
          <p className="muted">Can't scan? Enter this manually instead:</p>
          <p className="mfa-secret">{secretUri && new URL(secretUri.replace("otpauth://", "http://")).searchParams.get("secret")}</p>

          <FieldError>{error}</FieldError>

          <label htmlFor="mfa-code">Code from your app</label>
          <input
            id="mfa-code"
            type="text"
            inputMode="numeric"
            autoComplete="one-time-code"
            pattern="[0-9]*"
            maxLength={6}
            required
            placeholder="123456"
            value={code}
            onChange={(e) => setCode(e.target.value.replace(/\D/g, ""))}
          />
          <button type="submit" disabled={busy || code.length < 6}>
            {busy ? "Confirming…" : "Confirm and activate"}
          </button>
          <button type="button" className="ghost" onClick={() => setStep("identify")}>
            Back
          </button>
        </form>
      </main>
    );
  }

  return (
    <main className="auth">
      <form className="card" onSubmit={(e) => void requestSecret(e)}>
        <h1>Set up your authenticator</h1>
        <p className="muted">
          For a clinician who hasn't enrolled MFA yet — you'll need this before you can sign in.
        </p>

        <label htmlFor="mfa-email">Email</label>
        <input
          id="mfa-email"
          type="email"
          autoComplete="username"
          required
          value={email}
          onChange={(e) => setEmail(e.target.value)}
        />

        <PasswordField
          id="mfa-password"
          label="Password"
          autoComplete="current-password"
          required
          value={password}
          onChange={setPassword}
        />

        <FieldError>{error}</FieldError>

        <button type="submit" disabled={busy}>
          {busy ? "Checking…" : "Continue"}
        </button>
      </form>
    </main>
  );
}
