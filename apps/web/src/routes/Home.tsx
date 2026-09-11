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
 * - There was no button anywhere that created a new encounter. Neither the
 *   recording screen nor the (since-removed) in-app consent screen ever
 *   created one — both only read `:encounterId` from the URL. "Start a new
 *   consultation" below does what a doctor actually does first.
 *
 * Two gaps this redesign's own completeness audit found and closes:
 * - "Needs attention" listed failed encounters with no way to retry them —
 *   `POST /encounters/{id}/retry` existed and nothing called it.
 * - `compliance` is a real, seeded, RBAC-enforced role with nowhere to go —
 *   it landed on this exact doctor worklist. Redirected to `/audit` instead.
 */
import { useEffect, useState, type KeyboardEvent, type ReactNode } from "react";
import { useNavigate, Link } from "react-router-dom";
import { api, OfflineError } from "../api/client";
import { useAuth } from "../lib/auth";
import { useOnlineStatus } from "../lib/offline";
import { Banner, OfflineBanner } from "../components/Banner";
import { useToast } from "../components/Toast";
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

/**
 * Every folder-row's click destination, across all three tabs
 * (`/impeccable`: "make all encounter cards clickable regardless of their
 * status ... show their page based on the current status").
 *
 * A note or an active recording already have the right page — routing
 * those through EncounterDetail.tsx first would cost a click on the two
 * most common cases. Everything else (blocked-on-consent, failed,
 * mid-pipeline, an unlinked loose session) had no destination at all before
 * this; EncounterDetail is the one page all of those land on, adapting its
 * action to whichever status it's given rather than needing one page each.
 */
function destinationFor(e: Encounter): string {
  if (e.note_id) return `/notes/${e.note_id}`;
  if (e.pipeline_status === "recording") return `/encounters/${e.id}/record`;
  return `/encounters/${e.id}`;
}

/**
 * The clickable-row wrapper every tab now shares. Previously written inline,
 * once, only for "Recent" — now needed identically in all three, which is
 * exactly the "one-off implementation -> shared pattern" case rather than a
 * third copy of the same click/keydown/role/tabIndex wiring.
 */
function FolderRow({ to, children }: { to: string; children: ReactNode }) {
  const navigate = useNavigate();
  return (
    <li
      className="folder-row is-clickable"
      role="link"
      tabIndex={0}
      onClick={() => navigate(to)}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          navigate(to);
        }
      }}
    >
      {children}
    </li>
  );
}

export function Home() {
  const { signOut, role, name } = useAuth();
  const { showToast } = useToast();
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
      navigate(`/encounters/${res.data.id}/record`);
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
      // Low-stakes confirmation only: the row already disappearing from
      // "Needs attention" is the real signal this worked. If retrying fails
      // instead, that stays a persistent Banner (setError above), not a
      // toast — a doctor who misses this one loses nothing but a nicety.
      showToast("Retry queued.");
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
        <div className="header-actions">
          {/* Found by `/impeccable critique`: a shared clinic laptop with no
              on-screen answer to "whose account is this?" beyond a bare
              Sign out button. */}
          {name && <span className="muted">Signed in as {name}</span>}
          <button type="button" className="ghost" onClick={() => void signOut()}>
            Sign out
          </button>
        </div>
      </header>

      {!online && <OfflineBanner />}
      {error && <Banner tone="error">{error}</Banner>}

      <section className="card">
        <button type="button" onClick={() => void startConsultation()} disabled={starting}>
          {starting ? "Starting…" : "Start a new consultation"}
        </button>
        <p className="muted">Recording starts immediately — consent is handled outside the app.</p>
      </section>

      <QueueStatus entries={entries} storage={storage} onRetry={retry} onUploadNow={uploadNow} />

      {/* Recent is capped at 25 and scoped to this clinician (see the
          panel's own note below) — there was nowhere to go from here to
          find an older note, search by patient, or reach one a colleague
          filed. That's a separate screen, not a fourth tab: it's a
          clinic-wide search, not another slice of "my own worklist".
          Styled as a real `.ghost` action, not `.muted` body text
          (`/impeccable critique`) — this is a first-class capability, and
          every other call-to-action on this screen already wears the
          app's button vocabulary rather than plain paragraph styling.
          Placed *above* the tab rack, not wedged between it and the panel
          below (`/impeccable critique`, round 2): `.rack-tabs`/`.rack-panel`
          are deliberately flush against each other (the panel's own
          square top-left corner reads as "attached to the active tab
          above it") — squeezing content into that 0-margin gap broke the
          folder illusion and left this row with no breathing room on
          either side. */}
      <div className="loose-head">
        <span className="muted">Looking for something older, or a colleague's note?</span>
        <Link className="ghost" to="/notes">
          All notes
        </Link>
      </div>

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
        {/* Found by `/impeccable critique`: the folder-tab color family is
            learned once and reused everywhere (FolderTab.tsx's own header
            comment), but nothing ever explained the four colors themselves to
            someone seeing this rack for the first time. Each tab still
            carries its own text label — this is a legend for the color, not
            the only way to read it. Scoped to Recent: it's the only tab that
            ever shows all four colors — Loose sessions is always "blank" and
            Needs attention is always "attention". */}
        <p className="tab-legend muted">
          <span><span className="legend-swatch tab-progress" aria-hidden="true" />In progress</span>
          <span><span className="legend-swatch tab-done" aria-hidden="true" />Done</span>
          <span><span className="legend-swatch tab-attention" aria-hidden="true" />Needs attention</span>
          <span><span className="legend-swatch tab-hold" aria-hidden="true" />On hold</span>
        </p>
        <p className="muted">Your last 25 encounters, newest first.</p>
        {recent === null ? (
          <p className="muted">Loading…</p>
        ) : recent.length === 0 ? (
          <p className="muted">Nothing yet. Start a recording and it will appear here.</p>
        ) : (
          <ul className="loose">
            {recent.map((e) => {
              const seq = sequencePosition(e.pipeline_status, Boolean(e.note_id));
              const dest = destinationFor(e);
              return (
                <FolderRow key={e.id} to={dest}>
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
                  <div className="folder-actions">
                    {e.note_id ? (
                      // stopPropagation: the row above already navigates to
                      // the same place on click — without this, this nested
                      // link's own click bubbles up and fires that
                      // navigation too (harmless, same destination, but
                      // pointless).
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
                      // Found by `/impeccable`: this used to be a dead end —
                      // no note yet and no shortcut, so a failed or
                      // mid-pipeline row here had literally no action. The
                      // row itself is clickable now (destinationFor sends it
                      // to EncounterDetail), this just names what's there.
                      <span className="muted">
                        {PIPELINE_LABEL[e.pipeline_status] ?? "no note yet"}
                      </span>
                    )}
                  </div>
                </FolderRow>
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
        <p className="muted">Recordings not yet linked to a patient.</p>
        {loose === null ? (
          <p className="muted">Loading…</p>
        ) : loose.length === 0 ? (
          <p className="muted">None.</p>
        ) : (
          <ul className="loose">
            {loose.map((e) => (
              <FolderRow key={e.id} to={destinationFor(e)}>
                <FolderTab kind="blank" label="Unnamed" />
                <div className="folder-head">
                  <span className="folder-id">{e.id.slice(0, 8)}</span>
                  <div className="folder-actions">
                    <button
                      type="button"
                      className="ghost"
                      onClick={(ev) => {
                        // stopPropagation: this toggles an inline picker in
                        // place, it doesn't navigate — without this, every
                        // click here would also fire the row's own
                        // navigation to EncounterDetail underneath it.
                        ev.stopPropagation();
                        setLinkError(null);
                        setLinking(linking === e.id ? null : e.id);
                      }}
                    >
                      {linking === e.id ? "Cancel" : "Link to patient"}
                    </button>
                  </div>
                </div>
                {/* P0-6's one-tap linking action. Recording was never blocked
                    on identity, so this is where identity catches up.
                    stopPropagation on the wrapper: every click inside the
                    picker (typing, picking a candidate) must not also
                    navigate the row underneath it. */}
                {linking === e.id && (
                  <div onClick={(ev) => ev.stopPropagation()}>
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
                        // The row vanishing from "Loose sessions" already
                        // shows this worked; the toast just names who it was
                        // linked to, since the row itself never showed a name.
                        showToast(`Linked to ${p.full_name}.`);
                      }}
                    />
                  </div>
                )}
              </FolderRow>
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
          Encounters whose processing failed after all automatic retries. Each one can be retried by hand.
        </p>
        {failed === null ? (
          <p className="muted">Loading…</p>
        ) : failed.length === 0 ? (
          <p className="muted">Nothing failed.</p>
        ) : (
          <ul className="loose">
            {failed.map((e) => (
              <FolderRow key={e.id} to={destinationFor(e)}>
                <FolderTab kind="attention" label={PIPELINE_LABEL[e.pipeline_status] ?? e.pipeline_status} />
                <div className="folder-head">
                  <span className="folder-id">{e.id.slice(0, 8)}</span>
                  <div className="folder-actions">
                    {/* Secondary, not primary: every other per-row action in
                        this same folder-actions slot (Open note, Resume
                        recording, Link to patient) is `.ghost` — found while
                        making button styling uniform across the app.
                        stopPropagation: this retries in place, it doesn't
                        navigate — without it, every click here would also
                        fire the row's own navigation to EncounterDetail. */}
                    <button
                      type="button"
                      className="ghost"
                      disabled={retrying === e.id}
                      onClick={(ev) => {
                        ev.stopPropagation();
                        void retryPipeline(e.id);
                      }}
                    >
                      {retrying === e.id ? "Retrying…" : "Retry"}
                    </button>
                  </div>
                </div>
              </FolderRow>
            ))}
          </ul>
        )}
      </div>

      <section className="card">
        <h2>How this works</h2>
        <p className="muted">
          Starting a consultation above is the one entry point: recording, patient identity, and
          note review/edit/sign all follow from there, even if the wifi drops mid-visit. Once a
          note is drafted, tap any line to see — and hear — exactly where it came from before you
          sign it. Capture runs at mono Opus{" "}
          {TARGET_BITS_PER_SECOND / 1000} kbps (~{Math.round(estimatedBytesPerMinute() / 1024)} KB/min).
        </p>
      </section>
    </main>
  );
}
