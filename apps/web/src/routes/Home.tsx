/**
 * The signed-in app's home — the chart rack.
 *
 * The Patient Folder direction (apps/web/index.html) made this screen a
 * literal chart rack: every encounter is a folder, its pipeline_status a
 * colored tab on the folder itself, not a text badge tucked in a corner.
 *
 * ⚠️ Found live, deploying a demo, both fixed here:
 * - This file's own header comment used to claim patient identity (2.5),
 *   review/sign (2.6), and the grounding UI (Phase 3) were "not built yet" —
 *   stale since all three shipped; NoteReview.tsx alone is 370+ lines wiring
 *   real grounding, not a stub.
 * - There was no button anywhere that created a new encounter.
 *   `Consent.tsx` only ever reads `:encounterId` from the URL, it never
 *   creates one. "Start a new consultation" below does what a doctor
 *   actually does first.
 *
 * Two gaps this redesign's own completeness audit found and closes:
 * - "Needs attention" listed failed encounters with no way to retry them —
 *   `POST /encounters/{id}/retry` existed and nothing called it.
 * - `compliance` is a real, seeded, RBAC-enforced role with nowhere to go —
 *   it landed on this exact doctor worklist. Redirected to `/audit` instead.
 */
import { useEffect, useState, type KeyboardEvent } from "react";
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
import { FolderTab, StepSequence } from "../components/FolderTab";
import { encounterTab, sequencePosition, PIPELINE_LABEL, SEQUENCE_LENGTH } from "../lib/status-tab";

type Encounter = {
  id: string;
  pipeline_status: string;
  created_at: string;
  /** 1:1 with the encounter once a note exists — the only route into the
   *  review screen (Phase 2.6). */
  note_id?: string | null;
};

export function Home() {
  const { signOut, role } = useAuth();
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
  const [retrying, setRetrying] = useState<string | null>(null);
  // Loose / Recent / Needs attention used to be three sections stacked one
  // after another — a lot of scrolling once Recent has anywhere near its
  // full 25. Tabbed instead, using the same folder-tab shape the per-
  // encounter status already wears (apps/web/index.html's direction), just
  // toned neutrally rather than status-colored, so the two don't read as
  // the same kind of thing. "Needs attention" keeps a visible dot when it
  // has failures even while another tab is open — the one section a
  // doctor must not lose track of just because it isn't the active tab.
  const [activeTab, setActiveTab] = useState<"recent" | "loose" | "attention">("recent");

  // The compliance role has real RBAC-enforced routes (GET /audit-logs) but
  // never had a screen to reach them from — it landed here, on a worklist
  // meant for a doctor's own consultations. Route it to the surface built
  // for it instead of pretending it belongs on this one.
  useEffect(() => {
    if (role === "compliance") navigate("/audit", { replace: true });
  }, [role, navigate]);

  useEffect(() => {
    // A compliance account is redirected away by the effect above, but that
    // redirect doesn't stop this one from firing on the same mount — all
    // three of these are doctor-only (RBAC) and a compliance session got
    // three 403s out of every visit here for a screen it was never going
    // to see. Found by Playwright surfacing console errors, not by eye.
    if (role === "compliance") return;
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
  }, [role]);

  // The one action every downstream screen assumes already happened.
  // `crypto.randomUUID()` (browser-native, no dependency) matches
  // EncounterCreate's own doc comment — generated once per recording
  // session and replayed on every chunk/retry (P0-2) — so this key is
  // exactly the one that then flows through the rest of the queue.
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
      setError(
        e instanceof OfflineError
          ? "You're offline — reconnect to start a consultation."
          : "Could not start a new consultation. Try again.",
      );
    } finally {
      setStarting(false);
    }
  }

  async function retryPipeline(encounterId: string) {
    setRetrying(encounterId);
    setError(null);
    try {
      const res = await api.POST("/api/v1/encounters/{encounter_id}/retry", {
        params: { path: { encounter_id: encounterId } },
      });
      if (!res.data) {
        setError("Could not retry this encounter. Try again.");
        return;
      }
      setFailed((prev) => (prev ?? []).filter((e) => e.id !== encounterId));
    } catch (e) {
      setError(e instanceof OfflineError ? "You're offline — reconnect to retry." : "Could not retry this encounter.");
    } finally {
      setRetrying(null);
    }
  }

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

      {(() => {
        const tabs: { key: "recent" | "loose" | "attention"; label: string; count: number | null }[] = [
          { key: "recent", label: "Recent", count: recent?.length ?? null },
          { key: "loose", label: "Loose sessions", count: loose?.length ?? null },
          { key: "attention", label: "Needs attention", count: failed?.length ?? null },
        ];
        const activeIndex = tabs.findIndex((t) => t.key === activeTab);

        function onTabKeyDown(event: KeyboardEvent<HTMLDivElement>) {
          if (event.key !== "ArrowRight" && event.key !== "ArrowLeft") return;
          event.preventDefault();
          const delta = event.key === "ArrowRight" ? 1 : -1;
          const next = tabs[(activeIndex + delta + tabs.length) % tabs.length];
          setActiveTab(next.key);
          const el = document.getElementById(`worklist-tab-${next.key}`);
          el?.focus();
        }

        return (
          <div
            className="rack-tabs"
            role="tablist"
            aria-label="Encounter worklist"
            onKeyDown={onTabKeyDown}
          >
            {tabs.map((t) => (
              <button
                key={t.key}
                id={`worklist-tab-${t.key}`}
                type="button"
                role="tab"
                aria-selected={activeTab === t.key}
                aria-controls={`worklist-panel-${t.key}`}
                tabIndex={activeTab === t.key ? 0 : -1}
                className={
                  "rack-tab" +
                  (activeTab === t.key ? " is-active" : "") +
                  (t.key === "attention" && (failed?.length ?? 0) > 0 ? " has-attention" : "")
                }
                onClick={() => setActiveTab(t.key)}
              >
                {t.label}
                {t.count !== null && <span className="count"> ({t.count})</span>}
              </button>
            ))}
          </div>
        );
      })()}

      {/* Without this there was no way back to a note after filing it: the
          only lists were loose sessions and failures, so linking a patient
          removed an encounter from the one tray that showed it. Found by
          walking the onboarding runbook in a browser, not by a test. */}
      <div
        className="rack-panel"
        role="tabpanel"
        id="worklist-panel-recent"
        aria-labelledby="worklist-tab-recent"
        hidden={activeTab !== "recent"}
      >
        <p className="muted">Your last 25 encounters, newest first.</p>
        {recent === null ? (
          <p className="muted">Loading…</p>
        ) : recent.length === 0 ? (
          <p className="muted">Nothing yet. Start a recording and it will appear here.</p>
        ) : (
          <ul className="loose">
            {recent.map((e) => {
              const seq = sequencePosition(e.pipeline_status, Boolean(e.note_id));
              // A note exists -> open it. No note yet, but this encounter is
              // still at the very first pipeline stage -> nothing has left
              // this laptop yet, so it's resumable, not just viewable: back
              // into Record.tsx, which re-checks the consent gate itself on
              // mount and needs nothing carried over from this click. Every
              // later stage (uploaded/transcribed/generating) is mid-flight
              // on the server with no screen to land on, so those stay
              // un-clickable rather than linking somewhere that looks like
              // progress but shows nothing.
              const dest = e.note_id
                ? `/notes/${e.note_id}`
                : e.pipeline_status === "recording"
                  ? `/encounters/${e.id}/record`
                  : null;
              return (
                <li
                  key={e.id}
                  className={dest ? "folder-row is-clickable" : "folder-row"}
                  role={dest ? "link" : undefined}
                  tabIndex={dest ? 0 : undefined}
                  onClick={dest ? () => navigate(dest) : undefined}
                  onKeyDown={
                    dest
                      ? (event) => {
                          if (event.key === "Enter" || event.key === " ") {
                            event.preventDefault();
                            navigate(dest);
                          }
                        }
                      : undefined
                  }
                >
                  <FolderTab
                    kind={encounterTab(e.pipeline_status)}
                    label={PIPELINE_LABEL[e.pipeline_status] ?? e.pipeline_status}
                  />
                  <div className="folder-head">
                    <span className="folder-id">{e.id.slice(0, 8)}</span>
                    <span className="folder-date">{new Date(e.created_at).toLocaleDateString()}</span>
                  </div>
                  <StepSequence
                    length={SEQUENCE_LENGTH}
                    index={seq.index}
                    terminal={seq.terminal}
                    label={PIPELINE_LABEL[e.pipeline_status] ?? e.pipeline_status}
                  />
                  <div className="folder-actions" style={{ marginTop: ".6rem" }}>
                    {e.note_id ? (
                      // stopPropagation: the row above already navigates
                      // here on click — without this, this nested link's
                      // own click bubbles up and fires that navigation too
                      // (harmless, same destination, but pointless).
                      <Link className="ghost" to={`/notes/${e.note_id}`} onClick={(ev) => ev.stopPropagation()}>
                        Open note
                      </Link>
                    ) : e.pipeline_status === "recording" ? (
                      <Link
                        className="ghost"
                        to={`/encounters/${e.id}/record`}
                        onClick={(ev) => ev.stopPropagation()}
                      >
                        Resume recording
                      </Link>
                    ) : (
                      <span className="muted">no note yet</span>
                    )}
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </div>

      <div
        className="rack-panel"
        role="tabpanel"
        id="worklist-panel-loose"
        aria-labelledby="worklist-tab-loose"
        hidden={activeTab !== "loose"}
      >
        <p className="muted">Recordings not yet linked to a patient (P0-6).</p>
        {loose === null ? (
          <p className="muted">Loading…</p>
        ) : loose.length === 0 ? (
          <p className="muted">None.</p>
        ) : (
          <ul className="loose">
            {loose.map((e) => (
              <li key={e.id} className="folder-row">
                <FolderTab kind="blank" label="Unnamed" />
                <div className="folder-head">
                  <span className="folder-id">{e.id.slice(0, 8)}</span>
                  <div className="folder-actions">
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
      </div>

      <div
        className="rack-panel"
        role="tabpanel"
        id="worklist-panel-attention"
        aria-labelledby="worklist-tab-attention"
        hidden={activeTab !== "attention"}
      >
        <p className="muted">
          Encounters whose pipeline failed after all retries (Phase 1.5). Each one can be retried.
        </p>
        {failed === null ? (
          <p className="muted">Loading…</p>
        ) : failed.length === 0 ? (
          <p className="muted">Nothing failed.</p>
        ) : (
          <ul className="loose">
            {failed.map((e) => (
              <li key={e.id} className="folder-row">
                <FolderTab kind="attention" label={PIPELINE_LABEL[e.pipeline_status] ?? e.pipeline_status} />
                <div className="folder-head">
                  <span className="folder-id">{e.id.slice(0, 8)}</span>
                  <div className="folder-actions">
                    <button
                      type="button"
                      disabled={retrying === e.id}
                      onClick={() => void retryPipeline(e.id)}
                    >
                      {retrying === e.id ? "Retrying…" : "Retry"}
                    </button>
                  </div>
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>

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
