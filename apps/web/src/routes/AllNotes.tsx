/**
 * "All notes" — the searchable history the app was missing.
 *
 * The only lists before this were Home.tsx's chart rack: Recent (this
 * clinician's last 25 *encounters*), Loose, and Needs attention. Nothing let
 * a doctor find a note from two weeks ago, filter by patient, or reach one a
 * colleague filed — `GET /notes/search` (backend) is the first endpoint that
 * lists *notes* rather than encounters, and the first with real pagination.
 *
 * Unlike ComplianceAudit.tsx's filter form, this loads unfiltered on mount
 * rather than waiting for a submit: browsing "what's here" is the normal
 * case for this screen, and the query itself is not the sensitive part —
 * see the backend route's own docstring on why the typed name isn't what
 * gets audited.
 *
 * The audio action deliberately does not show a link inline for every row —
 * see AudioLinkButton.tsx for why: an audio URL is a live handle on PHI,
 * minted only on click.
 */
import { useCallback, useEffect, useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, OfflineError } from "../api/client";
import { useAuth } from "../lib/auth";
import { useOnlineStatus } from "../lib/offline";
import { Banner, OfflineBanner } from "../components/Banner";
import { FolderTab } from "../components/FolderTab";
import { AudioLinkButton } from "../components/AudioLinkButton";
import { noteTab, NOTE_STATUS_LABEL } from "../lib/status-tab";

type NoteStatusFilter = "" | "generated" | "filed" | "authenticated" | "signed";

type NoteRow = {
  note_id: string;
  encounter_id: string;
  patient_id: string | null;
  patient_name: string | null;
  note_status: "generated" | "filed" | "authenticated" | "signed";
  pipeline_status: string;
  created_at: string;
  signed_at: string | null;
};

const PAGE_SIZE = 50;

export function AllNotes() {
  const { signOut, name } = useAuth();
  const navigate = useNavigate();
  const online = useOnlineStatus();

  const [q, setQ] = useState("");
  const [status, setStatus] = useState<NoteStatusFilter>("");
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");

  const [rows, setRows] = useState<NoteRow[] | null>(null);
  const [total, setTotal] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);

  const search = useCallback(
    async (offset: number, append: boolean) => {
      if (append) setLoadingMore(true);
      else setBusy(true);
      setError(null);
      try {
        const { data, error: apiError, response } = await api.GET("/api/v1/notes/search", {
          params: {
            query: {
              q: q.trim() || undefined,
              status: status || undefined,
              date_from: dateFrom ? `${dateFrom}T00:00:00Z` : undefined,
              date_to: dateTo ? `${dateTo}T00:00:00Z` : undefined,
              limit: PAGE_SIZE,
              offset,
            },
          },
        });
        if (apiError || !data) {
          setError("Could not load notes for this query.");
          return;
        }
        const totalHeader = response.headers.get("X-Total-Count");
        setTotal(totalHeader !== null ? Number(totalHeader) : null);
        setRows((prev) => (append ? [...(prev ?? []), ...(data as NoteRow[])] : (data as NoteRow[])));
      } catch (e) {
        setError(e instanceof OfflineError ? "No connection — notes cannot be loaded." : "Could not load notes.");
      } finally {
        if (append) setLoadingMore(false);
        else setBusy(false);
      }
    },
    [q, status, dateFrom, dateTo],
  );

  useEffect(() => {
    // The one load this screen makes on its own — everything after is a
    // response to the doctor submitting the form or clicking "Load more".
    // eslint-disable-next-line react-hooks/exhaustive-deps
    void search(0, false);
  }, []);

  return (
    <main className="app">
      <header>
        <h1>All notes</h1>
        <div className="header-actions">
          {name && <span className="muted">Signed in as {name}</span>}
          <button type="button" className="ghost" onClick={() => void signOut()}>
            Sign out
          </button>
        </div>
      </header>

      <p className="muted">
        <Link to="/">← Back to worklist</Link>
      </p>

      {!online && <OfflineBanner />}
      {error && <Banner tone="error">{error}</Banner>}

      <section className="card">
        <h2>Search</h2>
        <p className="muted">Leave a field blank to widen the search. Runs on submit, not on every keystroke.</p>
        {/* Two rows, not four stacked full-width fields (`/impeccable
            critique`): this screen's own default is unfiltered browsing —
            forcing every visit past a tall stack of label/input pairs
            before the first real row contradicted that. Patient name and
            Status share a row (name is the field doctors actually type
            into, so it gets more of it); From/To are kept together on
            their own row rather than splitting across a wrap point — they
            read as one range, not two unrelated dates. */}
        <form
          className="search-form"
          onSubmit={(e: FormEvent) => {
            e.preventDefault();
            void search(0, false);
          }}
        >
          <div className="search-row">
            <div className="field field-wide">
              <label htmlFor="notes-q">Patient name</label>
              <input
                id="notes-q"
                type="text"
                placeholder="Typed or partial name"
                value={q}
                onChange={(e) => setQ(e.target.value)}
              />
            </div>
            <div className="field">
              <label htmlFor="notes-status">Status</label>
              <select id="notes-status" value={status} onChange={(e) => setStatus(e.target.value as NoteStatusFilter)}>
                <option value="">Any</option>
                <option value="generated">Ready to review</option>
                <option value="filed">Filed</option>
                <option value="authenticated">Authenticated</option>
                <option value="signed">Signed</option>
              </select>
            </div>
          </div>
          <div className="search-row">
            <div className="field">
              <label htmlFor="notes-from">From</label>
              <input id="notes-from" type="date" value={dateFrom} onChange={(e) => setDateFrom(e.target.value)} />
            </div>
            <div className="field">
              <label htmlFor="notes-to">To</label>
              <input id="notes-to" type="date" value={dateTo} onChange={(e) => setDateTo(e.target.value)} />
            </div>
            <button type="submit" disabled={busy}>
              {busy ? "Searching…" : "Search"}
            </button>
          </div>
        </form>
      </section>

      <section className="card">
        <h2>Results{total !== null && <span className="muted"> ({total})</span>}</h2>
        {rows === null ? (
          <p className="muted">Loading…</p>
        ) : rows.length === 0 ? (
          <p className="muted">Nothing matches this query.</p>
        ) : (
          <>
            <ul className="loose">
              {rows.map((r) => {
                const dest = `/notes/${r.note_id}`;
                return (
                  <li
                    key={r.note_id}
                    className="folder-row is-clickable"
                    role="link"
                    tabIndex={0}
                    onClick={() => navigate(dest)}
                    onKeyDown={(event) => {
                      if (event.key === "Enter" || event.key === " ") {
                        event.preventDefault();
                        navigate(dest);
                      }
                    }}
                  >
                    <FolderTab
                      kind={noteTab(r.note_status)}
                      label={NOTE_STATUS_LABEL[r.note_status] ?? r.note_status}
                    />
                    <div className="folder-head">
                      <span className="folder-patient">{r.patient_name ?? "Unlinked"}</span>
                      {/* Home's folder-rows always keep an id alongside whatever else is
                          shown — losing it here made two same-named patients (a real risk;
                          see PatientPicker.tsx) indistinguishable except by date. */}
                      <span className="folder-id">{r.note_id.slice(0, 8)}</span>
                      <span className="folder-date">{new Date(r.created_at).toLocaleDateString()}</span>
                    </div>
                    <div className="folder-actions" onClick={(ev) => ev.stopPropagation()}>
                      <Link className="ghost" to={dest}>
                        Open note
                      </Link>
                      <AudioLinkButton encounterId={r.encounter_id} />
                    </div>
                  </li>
                );
              })}
            </ul>
            {total !== null && rows.length < total && (
              <button type="button" className="ghost" disabled={loadingMore} onClick={() => void search(rows.length, true)}>
                {loadingMore ? "Loading…" : `Load more (${rows.length} of ${total})`}
              </button>
            )}
          </>
        )}
      </section>
    </main>
  );
}
