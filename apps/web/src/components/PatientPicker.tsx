/**
 * Name-first patient picker (P0-6).
 *
 * Search is debounced and manual-submit friendly rather than firing on every
 * keystroke: each search decrypts the whole directory server-side (see
 * `patient_matching.search_patients_by_name` and decision 0029), so
 * per-character requests would be both slow and a lot of PHI reads in the
 * audit log for one lookup.
 *
 * The exact-match case links without a confirmation step, per P0-6 — but
 * only when there is exactly one. Two patients with the identical name is
 * the case where silence attaches the note to the wrong person.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import {
  createPatient,
  searchPatients,
  type PatientHit,
  type SearchOutcome,
} from "../lib/patients";
import { Banner } from "./Banner";
import { useToast } from "./Toast";

const DEBOUNCE_MS = 400;

export function PatientPicker({
  onPicked,
  autoLinkExact = true,
}: {
  onPicked: (patient: { id: string; full_name: string; birthdate: string }) => void;
  /** P0-6's "links silently". Off where a deliberate choice is wanted. */
  autoLinkExact?: boolean;
}) {
  const { showToast } = useToast();
  const [query, setQuery] = useState("");
  const [outcome, setOutcome] = useState<SearchOutcome | null>(null);
  const [busy, setBusy] = useState(false);
  const [creating, setCreating] = useState(false);
  const [newBirthdate, setNewBirthdate] = useState("");
  const [createError, setCreateError] = useState<string | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  const run = useCallback(
    async (q: string) => {
      setBusy(true);
      const result = await searchPatients(q);
      setBusy(false);
      setOutcome(result);

      if (autoLinkExact && result.kind === "exact") {
        onPicked({
          id: result.patient.id,
          full_name: result.patient.full_name,
          birthdate: result.patient.birthdate,
        });
      }
    },
    [autoLinkExact, onPicked],
  );

  useEffect(() => {
    clearTimeout(timer.current);
    if (!query.trim()) {
      setOutcome(null);
      return;
    }
    timer.current = setTimeout(() => void run(query), DEBOUNCE_MS);
    return () => clearTimeout(timer.current);
  }, [query, run]);

  const onCreate = useCallback(async () => {
    setCreateError(null);
    if (!newBirthdate) {
      // Not optional: P0-6 requires name + birthdate for a new record,
      // because a name alone cannot deduplicate.
      setCreateError("A birthdate is required — a name alone cannot tell two patients apart.");
      return;
    }
    const result = await createPatient(query.trim(), newBirthdate);
    if (!result.ok) {
      setCreateError(result.reason);
      return;
    }
    onPicked({ id: result.id, full_name: query.trim(), birthdate: newBirthdate });
    // Low-stakes confirmation only: onPicked already drives whatever the
    // caller shows next (a linked banner, a row leaving a list) — this just
    // names the thing that quietly happened, a brand-new directory record.
    showToast(`Created patient: ${query.trim()}.`);
  }, [query, newBirthdate, onPicked]);

  return (
    <div className="picker">
      <label htmlFor="patient-q">Patient name</label>
      <input
        id="patient-q"
        type="text"
        autoComplete="off"
        placeholder="Type or dictate the name"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
      />
      {busy && <p className="muted">Searching…</p>}

      {outcome?.kind === "error" && <Banner tone="error">{outcome.reason}</Banner>}

      {outcome?.kind === "exact" && !autoLinkExact && (
        <Candidates
          heading="Exact match"
          hits={[outcome.patient]}
          onPick={onPicked}
        />
      )}

      {outcome?.kind === "near" && (
        <Candidates
          heading="Is it one of these?"
          hint="Check the birthdate — similar names are exactly where a note gets attached to the wrong person."
          hits={outcome.candidates}
          onPick={onPicked}
        />
      )}

      {outcome?.kind === "none" && query.trim() && (
        <div className="picker-create">
          <p className="muted">
            No match for “{query.trim()}”. Creating a record needs a birthdate as well as the name.
          </p>
          {!creating ? (
            <button type="button" onClick={() => setCreating(true)}>
              Create a new patient
            </button>
          ) : (
            <>
              <label htmlFor="patient-dob">Birthdate</label>
              <input
                id="patient-dob"
                type="date"
                value={newBirthdate}
                onChange={(e) => setNewBirthdate(e.target.value)}
              />
              {createError && <Banner tone="error">{createError}</Banner>}
              <button type="button" onClick={() => void onCreate()}>
                Create “{query.trim()}”
              </button>
            </>
          )}
        </div>
      )}
    </div>
  );
}

function Candidates({
  heading,
  hint,
  hits,
  onPick,
}: {
  heading: string;
  hint?: string;
  hits: PatientHit[];
  onPick: (p: { id: string; full_name: string; birthdate: string }) => void;
}) {
  return (
    <div className="candidates">
      <h3>{heading}</h3>
      {hint && <p className="muted">{hint}</p>}
      <ul>
        {hits.map((hit) => (
          <li key={hit.id}>
            <button
              type="button"
              className="candidate"
              onClick={() =>
                onPick({ id: hit.id, full_name: hit.full_name, birthdate: hit.birthdate })
              }
            >
              <span className="candidate-name">{hit.full_name}</span>
              <span className="candidate-dob">born {hit.birthdate}</span>
              {hit.match_type === "exact" && <span className="candidate-tag">exact</span>}
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
