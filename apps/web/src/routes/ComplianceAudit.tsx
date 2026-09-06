/**
 * The compliance/audit screen — did not exist before this redesign.
 *
 * `compliance` ("read and audit only") has been a real, seeded, RBAC-
 * enforced role since Phase 4.2 — `GET /audit-logs` requires exactly
 * `compliance` or `admin` — but no screen ever called it. Every compliance
 * account landed on the doctor's own worklist with nothing to do there.
 *
 * Filters mirror the route's own query params directly rather than
 * inventing a friendlier vocabulary the backend doesn't share, because a
 * compliance reviewer's actual question is usually "everything that
 * touched entity X" or "everything actor Y did", in those exact terms —
 * see `docs/decisions` on why audit rows are structured this way.
 */
import { useCallback, useState, type FormEvent } from "react";
import { useAuth } from "../lib/auth";
import { api, OfflineError } from "../api/client";
import { Banner, OfflineBanner } from "../components/Banner";
import { useOnlineStatus } from "../lib/offline";

type AuditRow = {
  id: string;
  actor_clinician_id: string | null;
  action: string;
  entity_type: string;
  entity_id: string;
  created_at: string;
  diff: string | null;
  retention_expires_at: string;
};

export function ComplianceAudit() {
  const { signOut, name } = useAuth();
  const online = useOnlineStatus();
  const [entityType, setEntityType] = useState("");
  const [entityId, setEntityId] = useState("");
  const [actionPrefix, setActionPrefix] = useState("");
  const [actorId, setActorId] = useState("");
  const [rows, setRows] = useState<AuditRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const search = useCallback(async (event?: FormEvent) => {
    event?.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const { data, error: apiError } = await api.GET("/api/v1/audit-logs", {
        params: {
          query: {
            entity_type: entityType.trim() || undefined,
            entity_id: entityId.trim() || undefined,
            action_prefix: actionPrefix.trim() || undefined,
            actor_clinician_id: actorId.trim() || undefined,
            limit: 50,
          },
        },
      });
      if (apiError || !data) {
        setError("Could not load audit rows for this query.");
        return;
      }
      setRows(data as AuditRow[]);
    } catch (e) {
      setError(e instanceof OfflineError ? "No connection — the audit log cannot be reached." : "Could not load audit rows.");
    } finally {
      setBusy(false);
    }
  }, [entityType, entityId, actionPrefix, actorId]);

  return (
    <main className="app">
      <header>
        <h1>Audit log</h1>
        <div style={{ display: "flex", alignItems: "center", gap: ".7rem" }}>
          {name && <span className="muted">Signed in as {name}</span>}
          <button type="button" className="ghost" onClick={() => void signOut()}>
            Sign out
          </button>
        </div>
      </header>

      {!online && <OfflineBanner />}

      <section className="card">
        <h2>Filter</h2>
        <p className="muted">
          Every disclosure of, or capability over, PHI is recorded here (checklist 4.2) — 22 of 23
          PHI-facing endpoints. Leave a field blank to widen the search.
        </p>
        <form onSubmit={(e) => void search(e)}>
          <label htmlFor="entity-type">Entity type</label>
          <input
            id="entity-type"
            type="text"
            placeholder="e.g. patient, note, encounter"
            value={entityType}
            onChange={(e) => setEntityType(e.target.value)}
          />
          <label htmlFor="entity-id">Entity id</label>
          <input
            id="entity-id"
            type="text"
            placeholder="Everything done to this one record"
            value={entityId}
            onChange={(e) => setEntityId(e.target.value)}
          />
          <label htmlFor="action-prefix">Action prefix</label>
          <input
            id="action-prefix"
            type="text"
            placeholder='e.g. "note." or "encounter.upload."'
            value={actionPrefix}
            onChange={(e) => setActionPrefix(e.target.value)}
          />
          <label htmlFor="actor-id">Clinician id</label>
          <input
            id="actor-id"
            type="text"
            placeholder="Everything this clinician did"
            value={actorId}
            onChange={(e) => setActorId(e.target.value)}
          />
          <button type="submit" disabled={busy}>
            {busy ? "Searching…" : "Search"}
          </button>
        </form>
      </section>

      {error && <Banner tone="error">{error}</Banner>}

      <section className="card">
        <h2>Results</h2>
        {rows === null ? (
          <p className="muted">Run a search above — the first search happens on demand, not on load.</p>
        ) : rows.length === 0 ? (
          <p className="muted">Nothing matches this query.</p>
        ) : (
          <div className="table-scroll">
            <table className="audit-table">
              <thead>
                <tr>
                  <th>When</th>
                  <th>Actor</th>
                  <th>Action</th>
                  <th>Entity</th>
                  <th>Retention</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr key={row.id}>
                    <td>{new Date(row.created_at).toLocaleString()}</td>
                    <td>{row.actor_clinician_id ? row.actor_clinician_id.slice(0, 8) : "system"}</td>
                    <td>{row.action}</td>
                    <td>
                      {row.entity_type}/{row.entity_id.slice(0, 8)}
                    </td>
                    <td>{new Date(row.retention_expires_at).toLocaleDateString()}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </main>
  );
}
