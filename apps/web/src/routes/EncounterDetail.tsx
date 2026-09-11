/**
 * One encounter's status, in full — the page every worklist card now opens
 * onto (`/impeccable`: "make all encounter cards clickable regardless of
 * their status ... show their page based on the current status").
 *
 * Before this, three of Home.tsx's rows had nowhere to go at all: a failed
 * encounter in "Recent" (as opposed to the separate "Needs attention" tab),
 * a loose session with no note yet, and anything mid-pipeline (uploaded /
 * transcribed, waiting on a worker). Rather than inventing a different
 * destination per status, this is the one page every status renders onto —
 * the folder-tab and step-sequence stay identical to how the worklist rows
 * already show status, and only the action card below them changes shape.
 *
 * Two statuses still route straight past this page from Home.tsx (a note ->
 * NoteReview, an active recording -> Record.tsx) because those pages are
 * already the right destination and a detour through here would cost a
 * click for the two most common cases. Everything else lands here.
 */
import { useCallback, useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api, OfflineError } from "../api/client";
import { Banner, OfflineBanner } from "../components/Banner";
import { PatientPicker } from "../components/PatientPicker";
import { FolderTab, StepSequence } from "../components/FolderTab";
import { useOnlineStatus } from "../lib/offline";
import { fetchPatient, linkEncounterToPatient } from "../lib/patients";
import { encounterTab, sequencePosition, PIPELINE_LABEL, SEQUENCE_LENGTH } from "../lib/status-tab";

type Encounter = {
  id: string;
  patient_id: string | null;
  pipeline_status: string;
  retry_count: number;
  last_pipeline_error: string | null;
  note_id: string | null;
  created_at: string;
};

const FAILED_STATUSES = new Set(["transcription_failed", "generation_failed"]);
const PENDING_STATUSES = new Set(["uploaded", "transcribed"]);

export function EncounterDetail() {
  const { encounterId = "" } = useParams();
  const navigate = useNavigate();
  const online = useOnlineStatus();
  const [encounter, setEncounter] = useState<Encounter | null>(null);
  const [patient, setPatient] = useState<{ full_name: string; birthdate: string } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [retrying, setRetrying] = useState(false);
  const [linking, setLinking] = useState(false);

  const load = useCallback(async () => {
    try {
      const { data, error: apiError } = await api.GET("/api/v1/encounters/{encounter_id}", {
        params: { path: { encounter_id: encounterId } },
      });
      if (apiError || !data) {
        setError("Could not load this encounter.");
        return;
      }
      const loaded = data as Encounter;
      setEncounter(loaded);
      // Additive, same as NoteReview's own patient fetch: a failed name
      // lookup leaves the rest of the page fully usable.
      setPatient(loaded.patient_id ? await fetchPatient(loaded.patient_id) : null);
    } catch (e) {
      setError(e instanceof OfflineError ? "No connection — this encounter cannot be loaded." : "Could not load this encounter.");
    }
  }, [encounterId]);

  useEffect(() => {
    void load();
  }, [load]);

  const retry = useCallback(async () => {
    setRetrying(true);
    setError(null);
    try {
      const { data, error: apiError } = await api.POST("/api/v1/encounters/{encounter_id}/retry", {
        params: { path: { encounter_id: encounterId } },
      });
      if (apiError || !data) {
        setError("Could not retry this encounter. Try again.");
        return;
      }
      setEncounter(data as Encounter);
    } catch (e) {
      setError(e instanceof OfflineError ? "You're offline — reconnect to retry." : "Could not retry this encounter.");
    } finally {
      setRetrying(false);
    }
  }, [encounterId]);

  if (error && !encounter) return <main className="app"><Banner tone="error">{error}</Banner></main>;
  if (!encounter) return <main className="app"><p className="muted">Loading…</p></main>;

  const status = encounter.pipeline_status;
  const seq = sequencePosition(status, Boolean(encounter.note_id));

  return (
    <main className="app">
      <header>
        <h1>Encounter</h1>
        <div className="header-actions">
          <code>{encounter.id.slice(0, 8)}</code>
          <button type="button" className="ghost" onClick={() => navigate("/")}>
            Back to worklist
          </button>
        </div>
      </header>

      {!online && <OfflineBanner />}
      {error && <Banner tone="error">{error}</Banner>}

      <section className="card status-card">
        <FolderTab kind={encounterTab(status)} label={PIPELINE_LABEL[status] ?? status} />
        <StepSequence
          length={SEQUENCE_LENGTH}
          index={seq.index}
          terminal={seq.terminal}
          label={PIPELINE_LABEL[status] ?? status}
        />
        <dl className="kv">
          <dt>Started</dt>
          <dd>{new Date(encounter.created_at).toLocaleString()}</dd>
          {encounter.retry_count > 0 && (
            <>
              <dt>Retried</dt>
              <dd>{encounter.retry_count} time{encounter.retry_count === 1 ? "" : "s"}</dd>
            </>
          )}
        </dl>
        {encounter.last_pipeline_error && (
          <Banner tone="error">{encounter.last_pipeline_error}</Banner>
        )}
      </section>

      {/* --- patient identity: shown if linked, linkable here if not --- */}
      <section className="card">
        <h2>Patient</h2>
        {encounter.patient_id ? (
          <p className="patient-identity">
            {patient?.full_name || "Name unavailable"}
            {patient?.birthdate && ` · born ${patient.birthdate}`}
          </p>
        ) : (
          <>
            <p className="muted">Not yet linked to a patient.</p>
            <PatientPicker
              autoLinkExact={false}
              onPicked={async (p) => {
                setLinking(true);
                setError(null);
                const ok = await linkEncounterToPatient(encounter.id, p.id);
                setLinking(false);
                if (!ok) {
                  setError("Could not link that patient. Try again.");
                  return;
                }
                setEncounter({ ...encounter, patient_id: p.id });
                setPatient({ full_name: p.full_name, birthdate: p.birthdate });
              }}
            />
            {linking && <p className="muted">Linking…</p>}
          </>
        )}
      </section>

      {/* --- the one action this status actually calls for --- */}
      <section className="card">
        <h2>Next step</h2>
        {encounter.note_id ? (
          <button type="button" onClick={() => navigate(`/notes/${encounter.note_id}`)}>
            Open note
          </button>
        ) : status === "recording" || status === "blocked_no_consent" ? (
          // blocked_no_consent is a leftover status from before the in-app
          // consent gate was removed — nothing blocks recording anymore, so
          // it resumes the same way an in-progress recording does.
          <button type="button" onClick={() => navigate(`/encounters/${encounter.id}/record`)}>
            Resume recording
          </button>
        ) : FAILED_STATUSES.has(status) ? (
          <>
            <p className="muted">
              Processing failed after every automatic retry. Retrying re-runs only the failed
              stage, not the whole pipeline.
            </p>
            <button type="button" disabled={retrying} onClick={() => void retry()}>
              {retrying ? "Retrying…" : "Retry"}
            </button>
          </>
        ) : PENDING_STATUSES.has(status) ? (
          <p className="muted">Still processing — nothing to do here yet.</p>
        ) : (
          <p className="muted">Nothing left to do for this encounter.</p>
        )}
      </section>
    </main>
  );
}
