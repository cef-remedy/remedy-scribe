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


export type WithdrawalResult =
  | {
      ok: true;
      /** Always "at the next stage boundary", never instantly. */
      pipelineWillStop: boolean;
      audioDeleted: boolean;
      nothingToDelete: boolean;
    }
  | { ok: false; reason: string };

/**
 * Submits a withdrawal (P0-1: "processing stops and the associated audio is
 * queued for deletion without undue delay").
 *
 * Returns what the server actually did rather than assuming success. The
 * doctor is standing in front of a patient who has just asked to stop being
 * recorded — "it's probably deleted" is not an acceptable thing to say, and
 * the UI needs the real answer to avoid saying it.
 */
export async function withdrawConsent(encounterId: string): Promise<WithdrawalResult> {
  try {
    const { data, error, response } = await api.POST("/api/v1/consent", {
      body: {
        encounter_id: encounterId,
        event: "withdrawn",
        participant_roster: [],
        purposes: [],
        script_language: "fil",
      },
    });

    if (error || !data || response.status !== 201) {
      return {
        ok: false,
        reason:
          "The withdrawal could not be saved to the server. Local audio has been deleted from this laptop, but tell the patient the server record is pending and retry.",
      };
    }

    const w = data.withdrawal;
    return {
      ok: true,
      pipelineWillStop: w?.pipeline_will_stop ?? false,
      audioDeleted: w?.audio_deleted ?? false,
      nothingToDelete: w?.nothing_to_delete ?? false,
    };
  } catch (e) {
    return {
      ok: false,
      reason:
        e instanceof OfflineError
          ? "No connection, so the withdrawal is not yet on the server. Local audio has been deleted from this laptop; retry once you are back online."
          : "The withdrawal could not be saved. Local audio has been deleted from this laptop.",
    };
  }
}

/**
 * Logs a fresh "given" event for a mid-visit re-consent (P0-1: "a new
 * participant joins mid-recording... recording pauses until fresh consent is
 * logged"). A new roster is the whole point — the ledger has to show who was
 * present for which stretch of the recording.
 */
export async function reconsent(
  encounterId: string,
  participants: string[],
  scriptLanguage: "fil" | "en",
): Promise<boolean> {
  try {
    const { response } = await api.POST("/api/v1/consent", {
      body: {
        encounter_id: encounterId,
        event: "given",
        participant_roster: participants,
        purposes: [],
        script_language: scriptLanguage,
      },
    });
    return response.status === 201;
  } catch {
    return false;
  }
}
