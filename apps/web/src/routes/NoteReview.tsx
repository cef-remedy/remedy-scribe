/**
 * Note review, edit, and signing (P0-5, checklist 2.6).
 *
 * Three things here are deliberate rather than incidental:
 *
 * 1. **Assessment → Plan → Subjective → Objective order**, not the SOAP
 *    order a clinician might expect. P0-4 specifies APSO because the
 *    doctor's own conclusion is what they check first; burying it under
 *    recounted symptoms is how a wrong assessment gets signed.
 *
 * 2. **Every edit is a revision**, saved on blur rather than on a single
 *    "Save" at the end. The edit-burden metric is the pilot's headline
 *    quality target, and a metric computed from one final diff cannot
 *    distinguish "the draft was nearly right" from "the doctor rewrote it in
 *    one pass".
 *
 * 3. **Grounding comes before editing** (Phase 3, P0-7). Each section renders
 *    as clickable lines first — tap one to see the transcript passage it came
 *    from, tap again to hear it — and swaps to a textarea only when the doctor
 *    chooses to edit. The first pass over an AI draft should be verification,
 *    and the default gesture should be the one the doctor is accountable for.
 *
 * 4. **Signing is a distinct ceremony**, not the last button in a row. It
 *    binds a PRC licence number to a real clinician, is irreversible, and
 *    makes the doctor — not the model — accountable for the content. It is
 *    visually separated and requires typing the licence number every time.
 */
import { useCallback, useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { api, OfflineError } from "../api/client";
import { Banner } from "../components/Banner";
import { PatientPicker } from "../components/PatientPicker";
import { GroundedSection } from "../components/GroundedSection";
import { audioNotice, fetchGrounding, type Grounding } from "../lib/grounding";
import { fetchPriorVisit, type PriorVisit } from "../lib/patients";
import { usePassagePlayer } from "../lib/usePassagePlayer";

type Section = "assessment" | "plan" | "subjective" | "objective";

type Note = {
  id: string;
  encounter_id: string;
  status: "generated" | "filed" | "authenticated" | "signed";
  assessment: string;
  plan: string;
  subjective: string;
  objective: string;
  note_generator_provider: string;
  prompt_version: string | null;
  signed_by_clinician_id: string | null;
  signed_prc_license_number: string | null;
  signed_at: string | null;
};

/** APSO, per P0-4 — see the note above on why not SOAP. */
const SECTIONS: { key: Section; label: string; hint: string }[] = [
  { key: "assessment", label: "Assessment", hint: "Your clinical conclusion. Checked first, on purpose." },
  { key: "plan", label: "Plan", hint: "What happens next." },
  { key: "subjective", label: "Subjective", hint: "What the patient reported." },
  {
    key: "objective",
    label: "Objective",
    hint: "Examination findings. Add anything you observed but never said aloud — the recording cannot know it.",
  },
];

const NEXT_STATUS: Record<Note["status"], Note["status"] | null> = {
  generated: "filed",
  filed: "authenticated",
  authenticated: "signed",
  signed: null,
};

export function NoteReview() {
  const { noteId = "" } = useParams();
  const [note, setNote] = useState<Note | null>(null);
  const [drafts, setDrafts] = useState<Record<Section, string>>({
    assessment: "",
    plan: "",
    subjective: "",
    objective: "",
  });
  const [prior, setPrior] = useState<PriorVisit | null>(null);
  const [patient, setPatient] = useState<{ id: string; full_name: string; birthdate: string } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);
  const [licence, setLicence] = useState("");
  const [savingSection, setSavingSection] = useState<Section | null>(null);
  const [grounding, setGrounding] = useState<Grounding | null>(null);
  const player = usePassagePlayer(note?.encounter_id ?? null);

  const load = useCallback(async () => {
    try {
      const { data, error: apiError } = await api.GET("/api/v1/notes/{note_id}", {
        params: { path: { note_id: noteId } },
      });
      if (apiError || !data) {
        setError("Could not load this note.");
        return;
      }
      const loaded = data as Note;
      setNote(loaded);
      setDrafts({
        assessment: loaded.assessment,
        plan: loaded.plan,
        subjective: loaded.subjective,
        objective: loaded.objective,
      });

      // Additive: a failed grounding read leaves the note fully readable and
      // signable. It must never be the reason a doctor cannot work.
      setGrounding(await fetchGrounding(noteId));

      const encounter = await api.GET("/api/v1/encounters/{encounter_id}", {
        params: { path: { encounter_id: loaded.encounter_id } },
      });
      const linkedPatientId = encounter.data?.patient_id ?? null;
      if (linkedPatientId) {
        setPatient({ id: linkedPatientId, full_name: "", birthdate: "" });
        setPrior(await fetchPriorVisit(linkedPatientId, loaded.encounter_id));
      }
    } catch (e) {
      setError(e instanceof OfflineError ? "No connection — this note cannot be loaded." : "Could not load this note.");
    }
  }, [noteId]);

  useEffect(() => {
    void load();
  }, [load]);

  /**
   * Saved on blur, one section at a time. Each PATCH creates a NoteRevision
   * server-side, which is what makes edit burden measurable at all.
   */
  const saveSection = useCallback(
    async (section: Section) => {
      if (!note || drafts[section] === note[section]) return;
      setSavingSection(section);
      setError(null);
      try {
        const { data, error: apiError, response } = await api.PATCH("/api/v1/notes/{note_id}", {
          params: { path: { note_id: noteId } },
          body: { section, text: drafts[section] },
        });
        if (apiError || !data) {
          setError(
            response.status === 409
              ? "This note is already signed and can no longer be edited."
              : "That edit could not be saved.",
          );
          return;
        }
        setNote(data as Note);
        // The edit may have shifted every offset after it, so the spans this
        // screen is highlighting by are now suspect. Re-ask the server rather
        // than keep rendering links it would no longer vouch for.
        setGrounding(await fetchGrounding(noteId));
      } catch {
        setError("That edit could not be saved — you may be offline.");
      } finally {
        setSavingSection(null);
      }
    },
    [note, drafts, noteId],
  );

  const advance = useCallback(async () => {
    if (!note) return;
    const to = NEXT_STATUS[note.status];
    if (!to) return;

    setError(null);
    setInfo(null);
    try {
      const { data, error: apiError, response } = await api.POST("/api/v1/notes/{note_id}/transition", {
        params: { path: { note_id: noteId } },
        body: {
          to_status: to,
          // Required only when signing; the server rejects a signature
          // without it rather than trusting the client to have asked.
          ...(to === "signed" ? { prc_license_number: licence.trim() } : {}),
          // Required only when filing (P0-6): identity is re-confirmed at
          // the moment the note is filed, not only at recording start.
          ...(to === "filed" ? { confirmed_patient_id: patient?.id ?? null } : {}),
        },
      });

      if (apiError || !data) {
        const detail = (apiError as { detail?: string } | undefined)?.detail;
        setError(
          response.status === 409
            ? (detail ?? "That step is not allowed from the note's current state.")
            : response.status === 422
              ? "A PRC licence number is required to sign."
              : "That step could not be completed.",
        );
        return;
      }
      setNote(data as Note);
      setLicence("");
      if (to === "signed") setInfo("Signed. This note is now part of the patient's permanent record.");
    } catch {
      setError("That step could not be completed — you may be offline.");
    }
  }, [note, noteId, licence, patient]);

  if (error && !note) return <main className="app"><Banner tone="error">{error}</Banner></main>;
  if (!note) return <main className="app"><p className="muted">Loading the note…</p></main>;

  const next = NEXT_STATUS[note.status];
  // The degradation ladder in words (Phase 3's heads-up): notes outlive audio,
  // and a doctor should know which rung they are on rather than meet a control
  // that quietly does nothing.
  const notice = grounding ? audioNotice(grounding.audio_state, grounding.transcript_state) : null;
  const signed = note.status === "signed";
  const needsPatient = note.status === "generated";

  return (
    <main className="app">
      <header>
        <h1>Review note</h1>
        <code>{note.status}</code>
      </header>

      {error && <Banner tone="error">{error}</Banner>}
      {info && <Banner tone="info">{info}</Banner>}

      {signed && (
        <Banner tone="info">
          Signed
          {note.signed_at ? ` on ${new Date(note.signed_at).toLocaleString()}` : ""} under PRC licence{" "}
          {note.signed_prc_license_number}. Signed notes are immutable.
        </Banner>
      )}

      {/* --- P0-6: identity re-confirmed at filing, not only at recording --- */}
      {needsPatient && (
        <section className="card">
          <h2>Confirm the patient</h2>
          <p className="muted">
            Checked again here, not just when recording started. Filing is the moment this note joins
            someone's permanent record — the last point a mis-linked recording is cheap to catch.
          </p>
          {patient?.id ? (
            <Banner tone="info">
              Linked to patient <code>{patient.id.slice(0, 8)}</code>. Filing will confirm this.
            </Banner>
          ) : (
            <PatientPicker onPicked={setPatient} />
          )}
        </section>
      )}

      {/* --- P0-5: prior visit's assessment and plan --- */}
      {prior && (
        <section className="card prior">
          <h2>Last visit</h2>
          <p className="muted">
            Signed {new Date(prior.signed_at).toLocaleDateString()}. Assessment and plan only —
            last visit's symptoms are not today's.
          </p>
          <dl className="kv">
            <dt>Assessment</dt>
            <dd className="prior-text">{prior.assessment || "—"}</dd>
            <dt>Plan</dt>
            <dd className="prior-text">{prior.plan || "—"}</dd>
          </dl>
        </section>
      )}

      {/* --- P0-7: where the note came from, before it is edited --- */}
      {notice && (
        <Banner tone="info">
          {notice}
        </Banner>
      )}
      {grounding && grounding.audio_state === "available" && (
        <p className="muted ground-help">
          Click any line of the note to see the transcript passage it was drafted from. Click it
          again to hear that moment of the consultation.
        </p>
      )}

      {/* --- the note itself, APSO --- */}
      {SECTIONS.map(({ key, label, hint }) => (
        <GroundedSection
          key={key}
          sectionKey={key}
          label={label}
          hint={hint}
          text={drafts[key]}
          savedText={note[key]}
          signed={signed}
          saving={savingSection === key}
          grounding={grounding}
          player={player}
          onChange={(text) => setDrafts((d) => ({ ...d, [key]: text }))}
          onBlur={() => void saveSection(key)}
        />
      ))}

      {/* --- the signing ceremony, deliberately separated --- */}
      {next === "signed" ? (
        <section className="card ceremony">
          <h2>Sign this note</h2>
          <p>
            Signing attaches <strong>your</strong> name and PRC licence to this content. It cannot be
            undone, and after it the note is immutable. Read the sections above before signing — you
            are accountable for them, not the model that drafted them.
          </p>
          <label htmlFor="prc">PRC licence number</label>
          <input
            id="prc"
            type="text"
            autoComplete="off"
            placeholder="e.g. 0123456"
            value={licence}
            onChange={(e) => setLicence(e.target.value)}
          />
          <button type="button" disabled={!licence.trim()} onClick={() => void advance()}>
            Sign as the responsible clinician
          </button>
        </section>
      ) : next ? (
        <section className="card">
          <h2>Next step</h2>
          <p className="muted">
            The note moves one step at a time — generated → filed → authenticated → signed, with no
            skipping. The server enforces that, not this screen.
          </p>
          <button
            type="button"
            disabled={next === "filed" && !patient?.id}
            onClick={() => void advance()}
          >
            {next === "filed" ? "Confirm patient and file" : `Mark as ${next}`}
          </button>
          {next === "filed" && !patient?.id && (
            <p className="muted">Confirm the patient above before filing.</p>
          )}
        </section>
      ) : null}

      <section className="card">
        <h2>Provenance</h2>
        <dl className="kv">
          <dt>Drafted by</dt>
          <dd>{note.note_generator_provider}</dd>
          <dt>Prompt version</dt>
          <dd>{note.prompt_version ?? "—"}</dd>
        </dl>
      </section>
    </main>
  );
}
