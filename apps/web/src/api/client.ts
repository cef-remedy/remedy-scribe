/**
 * The one place that talks to the API.
 *
 * Fully typed from the backend's own OpenAPI schema (`src/api/schema.d.ts`,
 * regenerated with `npm run api:types`) rather than hand-written fetch
 * calls. This is the payoff tech-stack.md promised for splitting a Python
 * backend from a TypeScript client: the route shapes are not duplicated
 * here, they are derived, so a backend change that breaks the client fails
 * at `tsc` instead of at runtime in a clinic.
 *
 * Token model (decisions 0006/0007, as amended by 0024):
 *   - access token  -> in memory only, never localStorage/sessionStorage.
 *                      Lost on reload, which is fine: the cookie below
 *                      silently restores the session.
 *   - refresh token -> httpOnly cookie, never visible to this code. That
 *                      is the whole point — XSS cannot read it, which
 *                      expo-secure-store could never promise.
 */
import createClient, { type Middleware } from "openapi-fetch";
import type { paths } from "./schema";

/**
 * Unset means "same origin" in a built bundle, and "the local API" in dev.
 *
 * The old default was `http://localhost:8000` unconditionally, which is
 * right for `npm run dev` and catastrophic for a static host: a Netlify
 * build with the variable simply *not created* produced a bundle that
 * compiled, deployed, loaded, and then asked every visitor's own machine
 * for the API. `apps/web/Dockerfile` already defends against this by
 * baking `VITE_API_BASE_URL=/`, but a Netlify build runs `npm run build`
 * directly and never sees that default — so the safe value has to live
 * here too, where forgetting is not an option.
 *
 * `/` rather than `""` because openapi-fetch strips the trailing slash to
 * `""` anyway, while an *empty* env var is exactly what a shell, a compose
 * file or Vite's env loading is liable to read as "unset".
 */
const BASE_URL =
  import.meta.env.VITE_API_BASE_URL ?? (import.meta.env.DEV ? "http://localhost:8000" : "/");

/** In-memory only. A module-scoped variable, deliberately not a store. */
let accessToken: string | null = null;

export function setAccessToken(token: string | null): void {
  accessToken = token;
}
export function getAccessToken(): string | null {
  return accessToken;
}

/**
 * Reads `role` out of the current access token — a routing hint for the UI
 * (which worklist to show), never a security boundary; the server enforces
 * RBAC on every route regardless of what this returns. Decoded on demand
 * rather than stored, so the token itself stays the only thing that ever
 * needs care — this just parses public claims already inside it.
 */
export function getClinicianRole(): string | null {
  if (!accessToken) return null;
  try {
    const payload = accessToken.split(".")[1];
    const json = JSON.parse(atob(payload.replace(/-/g, "+").replace(/_/g, "/")));
    return typeof json.role === "string" ? json.role : null;
  } catch {
    return null;
  }
}

/**
 * Same decode-on-demand approach as `getClinicianRole`, for the same reason:
 * a display hint the client reads out of a claim already in the token,
 * never a security boundary. Added so a shared clinic laptop can show whose
 * account is signed in (`/impeccable critique` found no screen ever did).
 */
export function getClinicianName(): string | null {
  if (!accessToken) return null;
  try {
    const payload = accessToken.split(".")[1];
    const json = JSON.parse(atob(payload.replace(/-/g, "+").replace(/_/g, "/")));
    return typeof json.full_name === "string" ? json.full_name : null;
  } catch {
    return null;
  }
}

/** Notified when the session is definitively gone, so the UI can redirect. */
type SessionEndedHandler = () => void;
let onSessionEnded: SessionEndedHandler = () => {};
export function setSessionEndedHandler(fn: SessionEndedHandler): void {
  onSessionEnded = fn;
}

/* ------------------------------------------------------------------ *
 * Single-flight refresh.
 *
 * This is not an optimization — it is required for correctness. Phase 0.3
 * rotates the refresh token on every use and treats a replayed one as a
 * stolen-token signal, revoking the entire session family. So two
 * concurrent refreshes are actively harmful: the second presents a token
 * the first already rotated, reuse detection fires, and the doctor is
 * logged out mid-consultation by our own client.
 *
 * A single shared promise means N concurrent 401s produce exactly one
 * refresh call, and everyone awaits the same result.
 * ------------------------------------------------------------------ */
let inFlightRefresh: Promise<boolean> | null = null;

export function refreshSession(): Promise<boolean> {
  if (inFlightRefresh) return inFlightRefresh;

  inFlightRefresh = (async () => {
    try {
      const response = await fetch(`${BASE_URL}/api/v1/auth/refresh`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        // Sends the httpOnly cookie. Without this the request carries no
        // credential at all and always 401s.
        credentials: "include",
        // Empty body on purpose: this client never sees the refresh token,
        // so it has nothing to put here. The server reads the cookie.
        body: "{}",
      });
      if (!response.ok) return false;
      const body = (await response.json()) as { access_token?: string };
      if (!body.access_token) return false;
      accessToken = body.access_token;
      scheduleProactiveRefresh();
      return true;
    } catch {
      // Network failure is not an auth failure. Keep whatever token we
      // have; the caller decides whether to surface an offline state.
      return false;
    } finally {
      inFlightRefresh = null;
    }
  })();

  return inFlightRefresh;
}

/* ------------------------------------------------------------------ *
 * Proactive renewal.
 *
 * Access tokens live 15 minutes (decision 0007). Waiting for a 401 means
 * every 15 minutes one unlucky request pays a double round-trip — and
 * during a recording upload that request might be a chunk. Renewing a
 * little early keeps the reactive path as a safety net rather than the
 * normal case.
 * ------------------------------------------------------------------ */
const ACCESS_TOKEN_LIFETIME_MS = 15 * 60 * 1000;
const RENEW_MARGIN_MS = 3 * 60 * 1000;
let refreshTimer: ReturnType<typeof setTimeout> | undefined;

function scheduleProactiveRefresh(): void {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(() => {
    void refreshSession();
  }, ACCESS_TOKEN_LIFETIME_MS - RENEW_MARGIN_MS);
}

export function stopProactiveRefresh(): void {
  clearTimeout(refreshTimer);
  refreshTimer = undefined;
}

/* ------------------------------------------------------------------ *
 * Auth-aware fetch, sitting *below* openapi-fetch so every generated
 * call gets it for free.
 * ------------------------------------------------------------------ */
const AUTH_PATHS = ["/api/v1/auth/login", "/api/v1/auth/refresh", "/api/v1/auth/logout"];

function isAuthPath(url: string): boolean {
  return AUTH_PATHS.some((p) => url.includes(p));
}

async function authedFetch(input: Request): Promise<Response> {
  const withCreds = (req: Request): Request => {
    const headers = new Headers(req.headers);
    if (accessToken) headers.set("authorization", `Bearer ${accessToken}`);
    return new Request(req, { headers, credentials: "include" });
  };

  let response = await fetch(withCreds(input.clone()));

  // Reactive renewal: one attempt, then give up. Retrying more than once
  // risks a loop against a server that 401s for a reason refresh cannot
  // fix (a deactivated clinician, say).
  if (response.status === 401 && !isAuthPath(input.url)) {
    const renewed = await refreshSession();
    if (renewed) {
      response = await fetch(withCreds(input.clone()));
    } else {
      onSessionEnded();
    }
  }

  return response;
}

/**
 * Surfaces a hard network failure distinctly from an HTTP error, so the UI
 * can say "you are offline" rather than "something went wrong" — the PRD is
 * explicit that nothing may fail silently.
 */
export class OfflineError extends Error {
  constructor() {
    super("Cannot reach the Remedy Scribe server.");
    this.name = "OfflineError";
  }
}

const authMiddleware: Middleware = {
  async onRequest({ request }) {
    return request;
  },
};

export const api = createClient<paths>({
  baseUrl: BASE_URL,
  fetch: async (request: Request) => {
    try {
      return await authedFetch(request);
    } catch {
      throw new OfflineError();
    }
  },
});
api.use(authMiddleware);

/* ------------------------------------------------------------------ *
 * Auth operations. Kept here rather than in a component so the token
 * never has to travel through React state.
 * ------------------------------------------------------------------ */

export type LoginResult =
  | { ok: true }
  | { ok: false; status: number; detail: string; retryAfterSeconds: number | null };

/**
 * A 422 deserves its own message. It means the request was malformed, not
 * that the credentials were wrong, and collapsing the two sends a doctor
 * to re-check their password when the real problem is the email address.
 * This is reachable by a real user, not just by a buggy client: `a@b.test`
 * passes the browser's own type="email" validation, and the API rejects
 * reserved TLDs (RFC 2606) via pydantic EmailStr.
 */
function describeLoginFailure(status: number, error: unknown): string {
  if (status === 401) return "Incorrect email, password, or authenticator code.";
  if (status === 429) return "Too many attempts. Wait a few minutes before trying again.";
  if (status === 422) {
    const mentionsEmail = JSON.stringify(error ?? "").toLowerCase().includes("email");
    return mentionsEmail
      ? "That email address is not accepted. Check it with your administrator."
      : "That sign-in request was rejected as invalid. Please check the fields and try again.";
  }
  if (status >= 500) return "The server had a problem. Please try again in a moment.";
  return "Could not sign in. Please try again.";
}

/**
 * `mfaCode` is optional: `settings.require_mfa` on the API (a demo-stage
 * toggle, `app/core/config.py`) decides whether it's checked at all, and
 * this client currently never has a code to send — see Login.tsx's own
 * header comment for how to restore the field if that toggle flips back.
 */
export async function login(email: string, password: string, mfaCode?: string): Promise<LoginResult> {
  const { data, error, response } = await api.POST("/api/v1/auth/login", {
    body: { email, password, mfa_code: mfaCode ?? null },
  });

  if (error || !data) {
    // Only meaningful on a 429 (see app/services/auth_rate_limit.py), and
    // only readable at all because main.py exposes it past CORS — a plain
    // fetch response hides any header not on the CORS-safelist otherwise.
    const rawRetryAfter = response.headers.get("retry-after");
    const parsedRetryAfter = rawRetryAfter === null ? NaN : Number(rawRetryAfter);
    return {
      ok: false,
      status: response.status,
      detail: describeLoginFailure(response.status, error),
      retryAfterSeconds: Number.isFinite(parsedRetryAfter) ? parsedRetryAfter : null,
    };
  }

  // data.refresh_token exists in the response body for non-browser callers.
  // This client deliberately ignores it — the httpOnly cookie the server
  // just set is the only copy we rely on.
  accessToken = data.access_token;
  scheduleProactiveRefresh();
  return { ok: true };
}

export type RegisterResult = { ok: true } | { ok: false; status: number; detail: string };

/**
 * Self-service account creation (`POST /auth/register`) — did not exist
 * before the free-tier demo; every account before this was seeded. Always
 * creates a `doctor` — the schema has no role field to send one, by
 * design (see the API's `RegisterRequest` docstring).
 */
export async function register(email: string, password: string, fullName: string): Promise<RegisterResult> {
  const { error, response } = await api.POST("/api/v1/auth/register", {
    body: { email, password, full_name: fullName },
  });

  if (error) {
    const detail =
      response.status === 409
        ? "An account with this email already exists."
        : response.status === 422
          ? "Check the email address and make sure the password is at least 8 characters."
          : "Could not create the account. Please try again.";
    return { ok: false, status: response.status, detail };
  }
  return { ok: true };
}

export async function logout(): Promise<void> {
  stopProactiveRefresh();
  try {
    await api.POST("/api/v1/auth/logout", { body: {} });
  } catch {
    // Network failure on logout still clears local state: leaving a stale
    // in-memory token behind is worse than a server-side session that
    // expires on its own.
  }
  accessToken = null;
}

/**
 * Called once at startup. There is no access token in memory after a
 * reload, but the httpOnly cookie may still be valid — so "am I logged
 * in?" is answered by asking the server, not by reading storage. This is
 * what makes resume-after-reload work without ever persisting a token
 * somewhere JS can read.
 */
export async function restoreSession(): Promise<boolean> {
  return refreshSession();
}
