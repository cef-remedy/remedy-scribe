/**
 * The consent screen (P0-1, checklist 2.3).
 *
 * Ordering is the thing to get right here, and P0-1 is stricter than it
 * first reads:
 *
 *   1. "when no consent record exists... the app blocks recording and
 *      presents the consent script (Filipino + English) **before anything
 *      is captured**"
 *   2. "**Given consent is given**, when recording starts, then the spoken
 *      exchange is captured as the **first segment** of the audio file"
 *
 * Both are satisfied only in this order: capture the roster, read the script
 * aloud, log the outcome to the ledger, and *only then* start recording — at
 * which point the doctor speaks a short confirmation that becomes segment 1.
 * Recording the asking itself would satisfy (2) while violating (1), so the
 * microphone is never touched on this screen.
 */
import { useCallback, useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api, OfflineError } from "../api/client";
import { Banner } from "../components/Banner";
import {
  CONSENT_PURPOSES,
  CONSENT_SCRIPTS,
  REQUIRED_PARTICIPANTS,
  SUGGESTED_PARTICIPANTS,
  type ScriptLanguage,
} from "../lib/consent-script";

type Step = "roster" | "script" | "submitting" | "declined";

export function Consent() {
  const { encounterId = "" } = useParams();
  const navigate = useNavigate();

  const [step, setStep] = useState<Step>("roster");
  const [extra, setExtra] = useState<string[]>([]);
  const [spokenLanguage, setSpokenLanguage] = useState<ScriptLanguage>("fil");
  const [error, setError] = useState<string | null>(null);

  const participants = useMemo(
    () => [...REQUIRED_PARTICIPANTS, ...extra],
    [extra],
  );

  const submit = useCallback(
    async (event: "given" | "declined") => {
      setStep("submitting");
      setError(null);
      try {
        const { error: apiError, response } = await api.POST("/api/v1/consent", {
          body: {
            encounter_id: encounterId,
            event,
            participant_roster: participants,
            purposes: [...CONSENT_PURPOSES],
            script_language: spokenLanguage,
          },
        });

        if (apiError || response.status !== 201) {
          setError(
            "Could not record the consent decision. Nothing has been recorded, and recording stays blocked.",
          );
          setStep("script");
          return;
        }

        if (event === "declined") {
          setStep("declined");
          return;
        }

        // Consent is now in the ledger, so the recording screen's gate will
        // open. It prompts for the spoken confirmation that becomes segment 1.
        navigate(`/encounters/${encounterId}/record?confirm=1`, { replace: true });
      } catch (e) {
        setError(
          e instanceof OfflineError
            ? "No connection, so the consent decision could not be saved. Recording stays blocked — consent has to be on the server before anything is captured."
            : "Could not record the consent decision. Recording stays blocked.",
        );
        setStep("script");
      }
    },
    [encounterId, participants, spokenLanguage, navigate],
  );

  if (step === "declined") {
    return (
      <main className="app">
        <header>
          <h1>Recording declined</h1>
        </header>
        <Banner tone="info">
          The decision is recorded. Nothing was captured.
        </Banner>
        <section className="card">
          <h2>The consultation continues as normal</h2>
          <p className="muted">
            Declining recording does not limit anything else — this is an explicit requirement, not a
            courtesy. Carry on with the consultation and write the note the usual way.
          </p>
          <button type="button" onClick={() => navigate("/", { replace: true })}>
            Back to worklist
          </button>
        </section>
      </main>
    );
  }

  return (
    <main className="app">
      <header>
        <h1>Consent to record</h1>
        <code>{encounterId.slice(0, 8)}</code>
      </header>

      <Banner tone="warn">
        <span>
          <strong>This script has not been cleared by counsel.</strong> RA 4200 clearance is a
          blocking open question owned by Legal — do not read this to a real patient until it is
          signed off. The mechanism is complete; the wording is a draft.
        </span>
      </Banner>

      {error && <Banner tone="error">{error}</Banner>}

      {/* --- step 1: who is in the room (RA 4200 needs every party) --- */}
      <section className="card">
        <h2>1 · Who is in the room?</h2>
        <p className="muted">
          RA 4200 requires the consent of <em>every</em> party to the conversation, so each person
          present is named on the ledger entry.
        </p>
        <ul className="roster">
          {REQUIRED_PARTICIPANTS.map((p) => (
            <li key={p}>
              <label>
                <input type="checkbox" checked disabled /> {p}{" "}
                <span className="muted">(always present)</span>
              </label>
            </li>
          ))}
          {SUGGESTED_PARTICIPANTS.map((p) => (
            <li key={p}>
              <label>
                <input
                  type="checkbox"
                  checked={extra.includes(p)}
                  onChange={(e) =>
                    setExtra((prev) => (e.target.checked ? [...prev, p] : prev.filter((x) => x !== p)))
                  }
                />{" "}
                {p}
              </label>
            </li>
          ))}
        </ul>
        {step === "roster" && (
          <button type="button" onClick={() => setStep("script")}>
            Continue to the script
          </button>
        )}
      </section>

      {step !== "roster" && (
        <>
          {/* --- step 2: read it aloud, in the language the patient understands --- */}
          <section className="card">
            <h2>2 · Read this aloud</h2>
            <p className="muted">
              Both versions are shown. Select the one you actually speak — it is stored on the
              ledger entry, because consent given in a language the patient does not understand is
              not consent.
            </p>
            <div className="lang-tabs" role="group" aria-label="Language spoken">
              {(["fil", "en"] as ScriptLanguage[]).map((lang) => (
                <button
                  key={lang}
                  type="button"
                  className={spokenLanguage === lang ? "lang-tab is-active" : "lang-tab"}
                  aria-pressed={spokenLanguage === lang}
                  onClick={() => setSpokenLanguage(lang)}
                >
                  {CONSENT_SCRIPTS[lang].label}
                  {spokenLanguage === lang ? " · spoken" : ""}
                </button>
              ))}
            </div>

            {(["fil", "en"] as ScriptLanguage[]).map((lang) => (
              <div
                key={lang}
                className={spokenLanguage === lang ? "script script--spoken" : "script"}
              >
                <h3>{CONSENT_SCRIPTS[lang].label}</h3>
                <ol>
                  {CONSENT_SCRIPTS[lang].lines.map((line, i) => (
                    <li key={i}>{line}</li>
                  ))}
                </ol>
                <p className="script-ask">{CONSENT_SCRIPTS[lang].askLine}</p>
              </div>
            ))}
          </section>

          {/* --- step 3: record the answer --- */}
          <section className="card">
            <h2>3 · What did the patient say?</h2>
            <p className="muted">
              Nothing has been captured yet. The microphone is not touched until after this is saved
              — P0-1 requires the script to come before any capture.
            </p>
            <div className="consent-actions">
              <button
                type="button"
                disabled={step === "submitting"}
                onClick={() => void submit("given")}
              >
                {step === "submitting" ? "Saving…" : "Patient agreed — start recording"}
              </button>
              <button
                type="button"
                className="ghost"
                disabled={step === "submitting"}
                onClick={() => void submit("declined")}
              >
                Patient declined
              </button>
            </div>
          </section>
        </>
      )}
    </main>
  );
}
