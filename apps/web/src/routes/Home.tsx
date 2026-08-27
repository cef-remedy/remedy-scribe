/**
 * Placeholder shell for the signed-in app. Phase 2.2 puts recording here;
 * 2.5 patient identity; 2.6 review/sign. Deliberately does not pretend to
 * offer features that do not exist yet — it names what is wired and what
 * is not, so a walkthrough cannot mistake a stub for a build.
 */
import { useEffect, useState } from "react";
import { api, OfflineError } from "../api/client";
import { useAuth } from "../lib/auth";
import { useOnlineStatus } from "../lib/offline";
import { Banner, OfflineBanner } from "../components/Banner";
import { QueueStatus } from "../components/QueueStatus";
import { useQueue } from "../lib/queue/useQueue";
import { estimatedBytesPerMinute, TARGET_BITS_PER_SECOND } from "../lib/audio-config";

type Encounter = { id: string; pipeline_status: string; created_at: string };

export function Home() {
  const { signOut } = useAuth();
  const online = useOnlineStatus();
  const { entries, storage, retry, uploadNow } = useQueue();
  const [loose, setLoose] = useState<Encounter[] | null>(null);
  const [failed, setFailed] = useState<Encounter[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;

    async function load() {
      try {
        // Both calls are fully typed from the OpenAPI schema: renaming
        // either route or field on the backend breaks `tsc` here.
        const [looseRes, failedRes] = await Promise.all([
          api.GET("/api/v1/encounters/loose"),
          api.GET("/api/v1/encounters/failed"),
        ]);
        if (cancelled) return;
        if (looseRes.data) setLoose(looseRes.data as Encounter[]);
        if (failedRes.data) setFailed(failedRes.data as Encounter[]);
        if (looseRes.error || failedRes.error) setError("Could not load your worklist.");
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

      <QueueStatus entries={entries} storage={storage} onRetry={retry} onUploadNow={uploadNow} />

      <section className="card">
        <h2>Loose sessions</h2>
        <p className="muted">Recordings not yet linked to a patient (P0-6).</p>
        {loose === null ? (
          <p className="muted">Loading…</p>
        ) : loose.length === 0 ? (
          <p className="muted">None.</p>
        ) : (
          <ul>
            {loose.map((e) => (
              <li key={e.id}>
                <code>{e.id.slice(0, 8)}</code> — {e.pipeline_status}
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
        <h2>Not built yet</h2>
        <ul className="muted">
          <li>Patient search and identity matching — Phase 2.5.</li>
          <li>Note review, editing, and signing — Phase 2.6.</li>
          <li>Grounding UI (tap a note line, hear the audio) — Phase 3.</li>
          <li className="muted">Recording, consent, and the upload queue are built: capture runs at mono Opus
            {" "}{TARGET_BITS_PER_SECOND / 1000} kbps (~{Math.round(estimatedBytesPerMinute() / 1024)} KB/min).</li>
        </ul>
      </section>
    </main>
  );
}
