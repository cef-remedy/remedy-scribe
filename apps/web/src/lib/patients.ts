/**
 * Patient identity (P0-6).
 *
 * The three-way outcome the PRD specifies is the whole design:
 *
 *   exact match  -> link silently, no prompt
 *   near match   -> one-tap confirmation
 *   no match     -> offer create-new with name + birthdate
 *
 * Note where birthdate enters. Search takes a **name only**, because that
 * is what a doctor has when they start typing. Birthdate governs
 * *deduplication* — it is shown alongside every candidate so two similar
 * names can be told apart, and it is required to create a record, because
 * "dedup uses name + birthdate together, not name alone".
 */
import { api, OfflineError } from "../api/client";

export type PatientHit = {
  id: string;
  full_name: string;
  birthdate: string;
  score: number;
  match_type: "exact" | "near";
};

export type SearchOutcome =
  /** Exactly one exact hit: P0-6 says link silently, so no confirmation. */
  | { kind: "exact"; patient: PatientHit }
  /** Candidates the doctor must choose between, or confirm one of. */
  | { kind: "near"; candidates: PatientHit[] }
  /** Nothing plausible: offer create-new. */
  | { kind: "none" }
  | { kind: "error"; reason: string };

export async function searchPatients(query: string): Promise<SearchOutcome> {
  if (!query.trim()) return { kind: "none" };

  try {
    const { data, error, response } = await api.GET("/api/v1/patients/search", {
      params: { query: { q: query, limit: 10 } },
    });

    if (error || !data) {
      return {
        kind: "error",
        reason:
          response.status === 403
            ? "Your account cannot search patients."
            : "Could not search the patient directory.",
      };
    }

    const hits = data as PatientHit[];
    const exact = hits.filter((h) => h.match_type === "exact");

    // "Links silently" applies only when there is exactly ONE exact match.
    // Two patients with the identical name is precisely the case where
    // silence would attach the note to the wrong person, so it falls through
    // to confirmation instead.
    if (exact.length === 1) return { kind: "exact", patient: exact[0] };
    if (hits.length > 0) return { kind: "near", candidates: hits };
    return { kind: "none" };
  } catch (e) {
    return {
      kind: "error",
      reason:
        e instanceof OfflineError
          ? "No connection, so the patient directory cannot be searched. Recording is not blocked — the session lands in the loose-sessions tray."
          : "Could not search the patient directory.",
    };
  }
}

/**
 * Creates a patient. Requires birthdate, and the server re-runs
 * name+birthdate matching, so a race between two doctors creating the same
 * patient resolves to one record rather than two.
 */
export async function createPatient(
  fullName: string,
  birthdate: string,
): Promise<{ ok: true; id: string } | { ok: false; reason: string }> {
  try {
    const { data, error } = await api.POST("/api/v1/patients", {
      body: { name: fullName, birthdate },
    });
    if (error || !data) return { ok: false, reason: "Could not create the patient record." };
    return { ok: true, id: data.id };
  } catch {
    return { ok: false, reason: "Could not create the patient record while offline." };
  }
}

/** Links a loose session to a patient (P0-6's one-tap linking action). */
export async function linkEncounterToPatient(
  encounterId: string,
  patientId: string,
): Promise<boolean> {
  try {
    const { response } = await api.POST("/api/v1/encounters/{encounter_id}/link-patient", {
      params: { path: { encounter_id: encounterId } },
      body: { patient_id: patientId },
    });
    return response.ok;
  } catch {
    return false;
  }
}

/**
 * Resolves a patient id to a name (P0 gap found by `/impeccable critique`:
 * NoteReview re-opens an encounter that was already linked to a patient —
 * usually the common case, not the first-link one PatientPicker handles —
 * and had no route to ask who that is, so the signing screen showed a
 * truncated UUID instead of a name). Returns null rather than throwing so a
 * failed lookup degrades to "no name available" instead of blocking the
 * note, matching how `fetchPriorVisit` already treats its own failures.
 */
export async function fetchPatient(
  patientId: string,
): Promise<{ id: string; full_name: string; birthdate: string } | null> {
  try {
    const { data } = await api.GET("/api/v1/patients/{patient_id}", {
      params: { path: { patient_id: patientId } },
    });
    return data ?? null;
  } catch {
    return null;
  }
}

export type PriorVisit = {
  note_id: string;
  encounter_id: string;
  assessment: string;
  plan: string;
  signed_at: string;
};

/**
 * The prior visit's assessment and plan (P0-5). Returns null for a
 * first-time patient, which is an ordinary state rather than an error.
 */
export async function fetchPriorVisit(
  patientId: string,
  excludeEncounterId?: string,
): Promise<PriorVisit | null> {
  try {
    const { data } = await api.GET("/api/v1/patients/{patient_id}/prior-visit", {
      params: {
        path: { patient_id: patientId },
        query: excludeEncounterId ? { exclude_encounter_id: excludeEncounterId } : {},
      },
    });
    return (data as PriorVisit | null) ?? null;
  } catch {
    return null;
  }
}
