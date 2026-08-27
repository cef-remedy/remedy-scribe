/**
 * ┌───────────────────────────────────────────────────────────────────────┐
 * │  THIS SCRIPT IS A PLACEHOLDER. IT HAS NOT BEEN CLEARED BY COUNSEL.    │
 * └───────────────────────────────────────────────────────────────────────┘
 *
 * `remedy-scribe-prd.md` lists "Is the RA 4200 consent flow (Requirement
 * P0-1) cleared by Philippine counsel?" as an Open Question owned by Legal
 * and marked **Blocking — "recording feature cannot launch without this"**.
 * That question is still open.
 *
 * So: the wording below is a structurally-complete draft written by an
 * engineer, not legal advice, and it must not be read to a real patient
 * until Legal signs it off. It exists so the *mechanism* can be built,
 * tested, and demonstrated now — and it is deliberately isolated in this one
 * file, as data rather than as strings scattered through components, so
 * replacing it with counsel's text is an edit here and nothing else.
 *
 * What a reviewer should check it against (RA 4200 and the Data Privacy Act
 * both bear on this):
 *   - RA 4200 requires the consent of **all** parties to record a private
 *     communication. Hence the participant roster: every person in the room
 *     is named, and each must agree.
 *   - The DPA requires purpose specification, a retention period, and a
 *     statement of the data subject's rights — including withdrawal.
 *   - It must be delivered in a language the patient actually understands,
 *     which is why both are presented and why the app records *which* one
 *     was spoken (`script_language` on the ledger entry).
 */

export type ScriptLanguage = "fil" | "en";

export type ConsentScript = {
  language: ScriptLanguage;
  label: string;
  /** Read aloud, in order. Kept as discrete lines so the UI can number them. */
  lines: string[];
  askLine: string;
};

/** The default roster. RA 4200 needs every party, so both are non-optional. */
export const REQUIRED_PARTICIPANTS = ["Doctor", "Patient"] as const;

/** Common additions, offered as one tap rather than free text where possible. */
export const SUGGESTED_PARTICIPANTS = [
  "Companion / relative",
  "Interpreter",
  "Nurse",
  "Trainee / observer",
] as const;

/**
 * Purposes consented to, stored on the ledger entry. Deliberately narrow:
 * the PRD's Non-Goals exclude prescriptions and external EMR integration,
 * and a consent form that claims broader purposes than the system has would
 * be both wrong and a liability.
 */
export const CONSENT_PURPOSES = [
  "Recording this consultation as audio",
  "Automatic transcription of the recording",
  "Generating a draft clinical note the doctor reviews and signs",
] as const;

export const CONSENT_SCRIPTS: Record<ScriptLanguage, ConsentScript> = {
  fil: {
    language: "fil",
    label: "Filipino",
    lines: [
      "Bago tayo magsimula, gusto ko lang pong magpaalam. Gumagamit ang klinika ng app na nagre-record ng usapan natin ngayon.",
      "Ang recording ay ginagamit lamang para gumawa ng draft na medical notes. Ako pa rin po ang magrereview at magpipirma ng huling nota.",
      "Walang ibang makakakita ng recording maliban sa mga awtorisadong tauhan ng Remedy. Hindi po ito ibinabahagi sa labas ng klinika.",
      "Buburahin po ang audio matapos ang panahong itinakda ng patakaran ng klinika.",
      "Maaari po kayong tumanggi. Kung tatanggi kayo, tuloy pa rin ang konsulta — magsusulat lang po ako ng nota sa normal na paraan.",
      "Maaari rin po kayong magbawi ng pahintulot kahit anong oras, kahit habang nagre-record. Titigil po ang proseso at buburahin ang audio.",
    ],
    askLine: "Pumapayag po ba kayong i-record ang usapan natin ngayon?",
  },
  en: {
    language: "en",
    label: "English",
    lines: [
      "Before we start, I need to ask your permission. This clinic uses an app that records our conversation today.",
      "The recording is used only to produce a draft of my medical notes. I still review and sign the final note myself.",
      "No one outside authorised Remedy staff can access the recording. It is not shared outside the clinic.",
      "The audio is deleted after the retention period set by clinic policy.",
      "You may say no. If you decline, the consultation continues exactly as normal — I will simply write the note the usual way.",
      "You may also withdraw your permission at any time, including while we are recording. Processing stops and the audio is deleted.",
    ],
    askLine: "Do you agree to us recording this consultation?",
  },
};

/**
 * The short line the doctor speaks *after* consent is logged and recording
 * has started, so it lands as the first segment of the audio (P0-1).
 *
 * Note the ordering this implies, which is stricter than it first appears:
 * P0-1's first bullet says nothing may be captured before consent, and its
 * second says the spoken exchange is the first segment. Both are satisfied
 * only in this order — log consent, start recording, then speak this for the
 * record. Recording the *asking* would violate the first bullet.
 */
export function spokenConfirmation(
  language: ScriptLanguage,
  participants: string[],
): string {
  const who = participants.join(", ");
  return language === "fil"
    ? `Nagsisimula na po ang recording. Nasa kwarto po: ${who}. Nagbigay po ng pahintulot ang pasyente na i-record ang konsultasyon.`
    : `Recording is now starting. Present in the room: ${who}. The patient has given permission to record this consultation.`;
}
