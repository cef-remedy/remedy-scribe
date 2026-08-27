/**
 * The recording screen (checklist 2.2 / 2.3).
 *
 * The consent gate is the first thing that happens and the only path to the
 * record button. P0-1 requires the app to block recording "before anything
 * is captured", so the gate is checked before `getUserMedia` is ever called
 * — not after, and not in parallel — and re-checked at the moment of the tap,
 * because consent can be withdrawn while this screen sits open.
 *
 * Phase 2.3 added the three things that make the gate a workflow rather than
 * a wall:
 *   - a route into the real bilingual consent screen when consent is missing;
 *   - the spoken-confirmation prompt, shown only once recording is actually
 *     running, because P0-1 wants that exchange as the *first segment* and
 *     wants nothing captured before consent;
 *   - **pause for mid-visit re-consent** and **withdrawal**. Note that
 *     resuming is gated on the ledger entry landing, not on the doctor
 *     saying it did: resuming without one would leave a new participant
 *     unconsented, which is the exact situation the pause exists to prevent.
 */
import { useCallback, useEffect, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { checkConsentGate, reconsent, withdrawConsent, type ConsentGate } from "../lib/consent";
import {
  REQUIRED_PARTICIPANTS,
  SUGGESTED_PARTICIPANTS,
  spokenConfirmation,
} from "../lib/consent-script";
import { useRecorder } from "../lib/recorder/useRecorder";
import { RecordingIndicator } from "../components/RecordingIndicator";
import { Banner } from "../components/Banner";
import { formatBytes, formatDuration } from "../lib/format";
import { TARGET_BITS_PER_SECOND } from "../lib/audio-config";
import { useOnlineStatus } from "../lib/offline";

export function Record() {
  const { encounterId = "" } = useParams();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const online = useOnlineStatus();
  const { state, start, stop, pause, resume, isRecording } = useRecorder();
  const [gate, setGate] = useState<ConsentGate | null>(null);
  const [summary, setSummary] = useState<string | null>(null);
  const [withdrawal, setWithdrawal] = useState<string | null>(null);
  const [newParticipant, setNewParticipant] = useState<string>(SUGGESTED_PARTICIPANTS[0]);
  const [reconsentError, setReconsentError] = useState<string | null>(null);

  // Set by the consent screen's redirect. The doctor has just logged consent
  // and now needs to speak the confirmation that becomes segment 1 (P0-1).
  const promptConfirmation = searchParams.get("confirm") === "1";

  useEffect(() => {
    let cancelled = false;
    void checkConsentGate(encounterId).then((result) => {
      if (!cancelled) setGate(result);
    });
    return () => {
      cancelled = true;
    };
  }, [encounterId]);

  // Leaving mid-recording loses the un-flushed tail. The browser only allows
  // a generic prompt, but a generic prompt beats silent loss.
  useEffect(() => {
    if (!isRecording) return;
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault();
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [isRecording]);

  const onStart = useCallback(async () => {
    // Re-checked at the moment of the tap, not just on mount: consent can be
    // withdrawn while this screen sits open, and the mount-time answer would
    // be stale. Cheap, and it is the difference between a gate and a hint.
    const fresh = await checkConsentGate(encounterId);
    setGate(fresh);
    if (!fresh.allowed) return;

    setSummary(null);
    try {
      await start(encounterId);
    } catch {
      // The session already surfaced the reason in state.error.
    }
  }, [encounterId, start]);

  const onStop = useCallback(async () => {
    const result = await stop();
    if (!result) return;
    const missing = result.missingMs >= 1000 ? formatDuration(result.missingMs) : null;
    setSummary(
      missing
        ? `Saved ${result.chunkCount} pieces to this laptop. ${missing} of audio is missing — see below.`
        : `Saved ${result.chunkCount} pieces to this laptop, with no audio gaps detected.`,
    );
  }, [stop]);

  const onWithdraw = useCallback(async () => {
    // Order matters and is deliberate: stop capturing first, then destroy the
    // local copy, then tell the server. If the network call fails, the audio
    // is already gone from this laptop — the failure mode leaves *less* data
    // behind, not more.
    if (state.status === "recording" || state.status === "paused") {
      await stop();
    }
    let localDeleted = 0;
    try {
      const { deleteSession } = await import("../lib/recorder/store");
      localDeleted = await deleteSession(encounterId);
    } catch {
      /* reported below */
    }

    const result = await withdrawConsent(encounterId);
    setGate(await checkConsentGate(encounterId));

    if (!result.ok) {
      setWithdrawal(result.reason);
      return;
    }
    setWithdrawal(
      `Withdrawal recorded. ${localDeleted} piece${localDeleted === 1 ? "" : "s"} of audio deleted from this laptop. ` +
        (result.nothingToDelete
          ? "Nothing had been uploaded yet, so there is nothing on the server to delete."
          : result.audioDeleted
            ? "The uploaded audio has been deleted from the server."
            : "The uploaded audio is queued for deletion — the server could not delete it immediately.") +
        " Processing stops at the next stage boundary, not instantly — a transcription already running cannot be killed mid-flight.",
    );
  }, [encounterId, state.status, stop]);

  const onNewParticipant = useCallback(async () => {
    setReconsentError(null);
    // Pause FIRST. P0-1: "recording pauses until fresh consent is logged" —
    // the pause is the compliance action, so it must not wait on a network
    // round trip that might fail.
    await pause();
  }, [pause]);

  const onReconsentGiven = useCallback(async () => {
    const ok = await reconsent(
      encounterId,
      [...REQUIRED_PARTICIPANTS, newParticipant],
      "fil",
    );
    if (!ok) {
      setReconsentError(
        "Fresh consent could not be saved, so recording stays paused. Retry — resuming without a ledger entry would leave the new participant unconsented.",
      );
      return;
    }
    await resume();
  }, [encounterId, newParticipant, resume]);

  return (
    <main className="app">
      <RecordingIndicator
        active={state.status === "recording" || state.status === "paused"}
        paused={state.status === "paused"}
        elapsedMs={state.elapsedMs}
        missingMs={state.missingMs}
      />

      <header>
        <h1>Record consultation</h1>
        <code>{encounterId.slice(0, 8)}</code>
      </header>

      {!online && (
        <Banner tone="warn">
          No connection. Recording still works — audio is saved on this laptop and uploads later.
        </Banner>
      )}

      {state.error && <Banner tone="error">{state.error}</Banner>}

      {/* --- the consent gate (P0-1) --- */}
      {gate === null && <p className="muted">Checking consent for this encounter…</p>}

      {gate && !gate.allowed && (
        <Banner
          tone="error"
          action={
            gate.needsConsentFlow ? (
              <button
                type="button"
                onClick={() => navigate(`/encounters/${encounterId}/consent`)}
              >
                Capture consent
              </button>
            ) : undefined
          }
        >
          <span>
            <strong>Recording is blocked.</strong> {gate.reason}
          </span>
        </Banner>
      )}

      {withdrawal && <Banner tone="warn">{withdrawal}</Banner>}

      {/* --- controls --- */}
      <section className="card">
        <h2>Capture</h2>
        {gate?.allowed ? (
          <>
            <p className="muted">
              Mono Opus at {TARGET_BITS_PER_SECOND / 1000} kbps, encrypted on this laptop before it
              touches disk, written in 5-second pieces so a crash costs at most one piece.
            </p>
            {state.status === "recording" || state.status === "paused" ? (
              <div className="consent-actions">
                <button type="button" onClick={() => void onStop()}>
                  Stop recording
                </button>
                {state.status === "recording" && (
                  <button type="button" className="ghost" onClick={() => void onNewParticipant()}>
                    Someone joined — pause
                  </button>
                )}
                <button type="button" className="ghost danger" onClick={() => void onWithdraw()}>
                  Patient withdrew consent
                </button>
              </div>
            ) : (
              <button type="button" onClick={() => void onStart()} disabled={state.status === "starting"}>
                {state.status === "starting" ? "Starting…" : "Start recording"}
              </button>
            )}

            {/* P0-1: the spoken exchange is the FIRST segment of the audio, so
                this prompt appears only once recording is actually running. */}
            {promptConfirmation && state.status === "recording" && state.elapsedMs < 30000 && (
              <Banner tone="info">
                <span>
                  <strong>Say this now, for the record:</strong>{" "}
                  “{spokenConfirmation("fil", [...REQUIRED_PARTICIPANTS])}”
                </span>
              </Banner>
            )}

            {/* Mid-visit re-consent. The pause already happened; resuming is
                gated on the ledger entry, not on the doctor's word. */}
            {state.status === "paused" && (
              <div className="card" style={{ marginTop: "1rem" }}>
                <h2>Fresh consent needed</h2>
                <p className="muted">
                  Recording is paused. P0-1 requires a new ledger entry naming everyone now present
                  before it can resume — read the script again for the new participant.
                </p>
                <label className="inl" htmlFor="who-joined">
                  Who joined?
                </label>
                <br />
                <select
                  id="who-joined"
                  value={newParticipant}
                  onChange={(e) => setNewParticipant(e.target.value)}
                >
                  {SUGGESTED_PARTICIPANTS.map((p) => (
                    <option key={p} value={p}>
                      {p}
                    </option>
                  ))}
                </select>
                {reconsentError && <Banner tone="error">{reconsentError}</Banner>}
                <div className="consent-actions">
                  <button type="button" onClick={() => void onReconsentGiven()}>
                    They consented — resume
                  </button>
                  <button type="button" className="ghost danger" onClick={() => void onWithdraw()}>
                    They declined — stop and delete
                  </button>
                </div>
              </div>
            )}
          </>
        ) : (
          <p className="muted">
            The record control appears once consent has been captured for this encounter.
          </p>
        )}

        {summary && <Banner tone="info">{summary}</Banner>}
      </section>

      {/* --- live detail, shown while recording and after --- */}
      {state.status !== "idle" && (
        <section className="card">
          <h2>This recording</h2>
          <dl className="kv">
            <dt>Elapsed</dt>
            <dd>{formatDuration(state.elapsedMs)}</dd>
            <dt>Audio captured</dt>
            <dd>{formatDuration(state.capturedMs)}</dd>
            <dt>Missing</dt>
            <dd className={state.missingMs >= 1000 ? "bad" : undefined}>
              {formatDuration(state.missingMs)}
            </dd>
            {state.pausedMs > 0 && (
              <>
                <dt>Paused</dt>
                <dd>{formatDuration(state.pausedMs)}</dd>
              </>
            )}
            <dt>Saved on this laptop</dt>
            <dd>
              {state.chunkCount} pieces · {formatBytes(state.bytes)}
            </dd>
            {state.deviceLabel && (
              <>
                <dt>Microphone</dt>
                <dd>{state.deviceLabel}</dd>
              </>
            )}
            {state.mimeType && (
              <>
                <dt>Format</dt>
                <dd>{state.mimeType}</dd>
              </>
            )}
          </dl>

          {state.gaps.length > 0 && (
            <Banner tone="error">
              <span>
                <strong>
                  {state.gaps.length} gap{state.gaps.length === 1 ? "" : "s"} in the audio.
                </strong>{" "}
                {state.gaps.some((g) => g.cause === "suspend")
                  ? "The laptop went to sleep during the recording — most likely the lid was closed. No software can capture audio while the machine is suspended, so that time is genuinely missing from the record."
                  : "The audio pipeline stalled. That time is missing from the record."}
              </span>
            </Banner>
          )}

          {state.mismatches.length > 0 && (
            <Banner tone="warn">
              <span>
                The microphone ignored{" "}
                {state.mismatches.map((m) => `${m.field} (asked ${m.requested}, got ${m.actual})`).join(", ")}
                . Recording continues — the bitrate is set explicitly, so this does not inflate the
                upload.
              </span>
            </Banner>
          )}
        </section>
      )}
    </main>
  );
}
