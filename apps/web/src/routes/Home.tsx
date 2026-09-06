/**
 * The signed-in app's home / worklist.
 *
 * ⚠️ Found live, deploying a demo: this file's own header comment used to
 * claim patient identity, review/sign and the grounding UI were "not built
 * yet" — stale since Phase 2.5+2.6 and Phase 3 shipped (both are real,
 * tested: NoteReview.tsx is 370+ lines wiring `fetchGrounding`, not a
 * stub). Worse, there was no button anywhere on this page that created a
 * new encounter — `Consent.tsx` only ever *reads* `:encounterId` from the
 * URL, never creates one — so the only way into the real consent/record
 * flow was typing a manually-created encounter's URL by hand. Fixed both:
 * "Start a new consultation" below does what a doctor actually does first.
 */
import { useEffect, useState } from "react";
import { useNavigate, Link } from "react-router-dom";
import { api, OfflineError } from "../api/client";
import { useAuth } from "../lib/auth";
import { useOnlineStatus } from "../lib/offline";
import { Banner, OfflineBanner } from "../components/Banner";
import { QueueStatus } from "../components/QueueStatus";
import { useQueue } from "../lib/queue/useQueue";
import { PatientPicker } from "../components/PatientPicker";
import { linkEncounterToPatient } from "../lib/patients";
import { estimatedBytesPerMinute, TARGET_BITS_PER_SECOND } from "../lib/audio-config";

type Encounter = {
  id: string;
  pipeline_status: string;
  created_at: string;
  /** 1:1 with the encounter once a note exists — the only route into the
   *  review screen (Phase 2.6). */
  note_id?: string | null;
};

export function Home() {
  const { signOut } = useAuth();
  const navigate = useNavigate();
  const online = useOnlineStatus();
  const { entries, storage, retry, uploadNow } = useQueue();
  const [linking, setLinking] = useState<string | null>(null);
  const [linkError, setLinkError] = useState<string | null>(null);
  const [loose, setLoose] = useState<Encounter[] | null>(null);
  const [failed, setFailed] = useState<Encounter[] | null>(null);
  const [recent, setRecent] = useState<Encounter[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);

  // The one action every one of these screens assumes already happened:
  // `Consent.tsx` only ever reads `:encounterId` from the URL, it never
  // creates one. `crypto.randomUUID()` (browser-native, no dependency)
  // matches EncounterCreate's own doc comment — generated once per
  // recording session and replayed on every chunk/retry (P0-2), so this
  // key is exactly the one that flows through the rest of the queue.
  async function startConsultation() {
    setStarting(true);
    setError(null);
    try {
      const res = await api.POST("/api/v1/encounters", {
        body: { upload_idempotency_key: crypto.randomUUID() },
      });
      if (!res.data) {
        setError("Could not start a new consultation. Try again.");
        return;
      }
      navigate(`/encounters/${res.data.id}/consent`);
    } catch (e) {
      setError(e instanceof OfflineError ? "You're offline — reconnect to start a consultation." : "Could not start a new consultation. Try again.");
    } finally {
      setStarting(false);
    }
  }

  useEffect(() => {
    let cancelled = false;

    async function load() {
      try {
        // Both calls are fully typed from the OpenAPI schema: renaming
        // either route or field on the backend breaks `tsc` here.
        const [looseRes, failedRes, recentRes] = await Promise.all([
          api.GET("/api/v1/encounters/loose"),
          api.GET("/api/v1/encounters/failed"),
          api.GET("/api/v1/encounters/recent", { params: { query: { limit: 25 } } }),
        ]);
        if (cancelled) return;
        if (looseRes.data) setLoose(looseRes.data as Encounter[]);
        if (failedRes.data) setFailed(failedRes.data as Encounter[]);
        if (recentRes.data) setRecent(recentRes.data as Encounter[]);
        if (looseRes.error || failedRes.error || recentRes.error) {
          setError("Could not load your worklist.");
        }
      } catch (e) {
        if (cancelled) return;
        setError(e instanceof OfflineError ? null : "Could not load your worklist.");
      }
    }

    void load();
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <main className="app">
      <header>
        <h1>Remedy Scribe</h1>
        <button type="button" className="ghost" onClick={() => void signOut()}>
          Sign out
        </button>
      </header>

      {!online && <OfflineBanner />}
      {error && <Banner tone="error">{error}</Banner>}

      <section className="card">
        <button type="button" onClick={() => void startConsultation()} disabled={starting}>
          {starting ? "Starting…" : "Start a new consultation"}
        </button>
        <p className="muted">
          Consent first, then recording — the consent screen never lets the
          microphone open before the roster and script are logged (P0-1).
        </p>
      </section>

      <QueueStatus entries={entries} storage={storage} onRetry={retry} onUploadNow={uploadNow} />

      <section className="card">
        <h2>Loose sessions</h2>
        <p className="muted">Recordings not yet linked to a patient (P0-6).</p>
        {loose === null ? (
          <p className="muted">Loading…</p>
        ) : loose.length === 0 ? (
          <p className="muted">None.</p>
        ) : (
          <ul className="loose">
            {loose.map((e) => (
              <li key={e.id}>
                <div className="loose-head">
                  <code>{e.id.slice(0, 8)}</code>
                  <span className="muted">{e.pipeline_status}</span>
                  <button
                    type="button"
                    className="ghost"
                    onClick={() => {
                      setLinkError(null);
                      setLinking(linking === e.id ? null : e.id);
                    }}
                  >
                    {linking === e.id ? "Cancel" : "Link to patient"}
                  </button>
                </div>
                {/* P0-6's one-tap linking action. Recording was never blocked
                    on identity, so this is where identity catches up. */}
                {linking === e.id && (
                  <PatientPicker
                    autoLinkExact={false}
                    onPicked={async (p) => {
                      const ok = await linkEncounterToPatient(e.id, p.id);
                      if (!ok) {
                        setLinkError("Could not link that patient. Try again.");
                        return;
                      }
                      setLinking(null);
                      setLoose((prev) => (prev ?? []).filter((x) => x.id !== e.id));
                    }}
                  />
                )}
              </li>
            ))}
          </ul>
        )}
        {linkError && <Banner tone="error">{linkError}</Banner>}
      </section>

      {/* Without this there was no way back to a note after filing it: the
          only lists were loose sessions and failures, so linking a patient
          removed an encounter from the one tray that showed it. Found by
          walking the onboarding runbook in a browser, not by a test. */}
      <section className="card">
        <h2>Recent</h2>
        <p className="muted">Your last 25 encounters, newest first.</p>
        {recent === null ? (
          <p className="muted">Loading…</p>
        ) : recent.length === 0 ? (
          <p className="muted">Nothing yet. Start a recording and it will appear here.</p>
        ) : (
          <ul className="loose">
            {recent.map((e) => (
              <li key={e.id}>
                <div className="loose-head">
                  <code>{e.id.slice(0, 8)}</code>
                  <span className="muted">{e.pipeline_status}</span>
                  <span className="candidate-dob">
                    {new Date(e.created_at).toLocaleDateString()}
                  </span>
                  {e.note_id ? (
                    <Link className="ghost" to={`/notes/${e.note_id}`}>
                      Open note
                    </Link>
                  ) : (
                    <span className="muted">no note yet</span>
                  )}
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="card">
        <h2>Needs attention</h2>
        <p className="muted">
          Encounters whose pipeline failed after all retries (Phase 1.5). Each one can be retried.
        </p>
        {failed === null ? (
          <p className="muted">Loading…</p>
        ) : failed.length === 0 ? (
          <p className="muted">Nothing failed.</p>
        ) : (
          <ul>
            {failed.map((e) => (
              <li key={e.id}>
                <code>{e.id.slice(0, 8)}</code> — {e.pipeline_status}
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="card">
        <h2>How this works</h2>
        <p className="muted">
          Consent, recording, the offline upload queue, patient identity,
          note review/edit/sign, and the grounding UI (tap a note line to
          see and hear where it came from) are all built and wired end to
          end — starting a consultation above is the one entry point into
          all of it. Capture runs at mono Opus{" "}
          {TARGET_BITS_PER_SECOND / 1000} kbps (~{Math.round(estimatedBytesPerMinute() / 1024)} KB/min).
        </p>
      </section>
    </main>
  );
}
