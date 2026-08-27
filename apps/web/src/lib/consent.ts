/**
 * The client half of the P0-1 consent gate.
 *
 * P0-1: "Given a doctor taps 'Record,' when no consent record exists for
 * that encounter, then the app blocks recording and presents the consent
 * script (Filipino + English) **before anything is captured**."
 *
 * That last clause is why this exists in 2.2 rather than waiting for 2.3.
 * The server already refuses to finalize an upload or transcribe without
 * consent (Phase 0.1), but both of those happen *after* capture. Shipping a
 * recorder that can start without consulting the ledger would build exactly
 * the thing P0-1 forbids, on the promise of gating it in a later phase.
 *
 * The gate reads from the **server**, not from local state. A reload
 * mid-encounter loses local state while the ledger entry persists, so local
 * state fails open — the one direction a consent gate must never fail.
 */
import { api, OfflineError } from "../api/client";

export type ConsentGate =
  | { allowed: true; scriptLanguage: string | null }
  | { allowed: false; reason: string; needsConsentFlow: boolean };

/**
 * Asks whether recording this encounter is currently permitted.
 *
 * Fails closed on every uncertain path, including offline. That is a
 * deliberate UX cost: a doctor with no connection cannot start a *new*
 * consented recording. The alternative — assuming consent because we cannot
 * check — is unlawful recording under RA 4200, which is not a trade this
 * gate gets to make. Phase 2.3 can soften it by caching a *positive* ledger
 * read for the current encounter, which is safe because consent already
 * existed at the time it was read.
 */
export async function checkConsentGate(encounterId: string): Promise<ConsentGate> {
  try {
    const { data, error, response } = await api.GET("/api/v1/consent/{encounter_id}", {
      params: { path: { encounter_id: encounterId } },
    });

    if (error || !data) {
      if (response.status === 401) {
        return { allowed: false, reason: "Your session expired. Sign in again.", needsConsentFlow: false };
      }
      return {
        allowed: false,
        reason: "Could not confirm consent for this encounter, so recording is blocked.",
        needsConsentFlow: false,
      };
    }

    if (!data.can_record) {
      const withdrawn = data.latest_event === "withdrawn";
      const declined = data.latest_event === "declined";
      return {
        allowed: false,
        needsConsentFlow: true,
        reason: withdrawn
          ? "Consent was withdrawn for this encounter. Recording is blocked until fresh consent is captured."
          : declined
            ? "The patient declined recording. The app works normally without it."
            : "No consent has been captured for this encounter yet.",
      };
    }

    return { allowed: true, scriptLanguage: data.script_language ?? null };
  } catch (e) {
    return {
      allowed: false,
      needsConsentFlow: false,
      reason:
        e instanceof OfflineError
          ? "No connection, so consent cannot be confirmed. Recording is blocked — this fails closed on purpose."
          : "Could not confirm consent for this encounter, so recording is blocked.",
    };
  }
}
