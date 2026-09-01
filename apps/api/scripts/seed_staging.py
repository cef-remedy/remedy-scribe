"""Staging seed (Phase 5.3). Fills a staging database with synthetic data
that behaves like the real thing, so nobody ever needs production PHI to
demo, debug, or rehearse against.

This is a PHI-safety deliverable, not a convenience script. The reason a
clinic copies production down to staging is always the same -- staging is
too empty to reproduce anything -- so the way to stop that copy from
happening is to make staging *fuller* than production feels, not to write
a policy forbidding it. Everything below exists to remove the motive.

Same shape of argument as decision 0031's published dev secrets: an
enforceable control beats a stated intention.

## What "realistic" has to mean here, concretely

Three things, each of which a lorem-ipsum seed gets wrong in a way that
hides a real bug:

1. **Filipino naming patterns**, because the patient matcher is fuzzy
   `difflib` over decrypted names (decision 0029) and its failure modes are
   entirely name-shaped. `_PATIENTS` below is built as a set of adversarial
   pairs -- a compound surname, a `Ma.` abbreviation, a Jr./father
   collision, an n-with-tilde spelling variant, two people with an
   identical name and different birthdates -- and each row says which
   behaviour it exists to exercise. "Patient One / Patient Two" would make
   every one of those cases pass.

2. **Taglish consultation transcripts** with word-level timings and
   confidences, including deliberately low-confidence words so the
   `[INAUDIBLE]` suppression path (P0-4) is visible, because a staging
   transcript in clean English never exercises the thing this product is
   for.

3. **Notes whose citations actually resolve.** The note bodies are built by
   calling the *production* `build_sections` from
   `app/services/note_generation/shared.py`, not by re-deriving its span
   convention. `apps/web/smoke/seed_pipeline.py` (the dev-only fixture this
   script grew out of) duplicates that convention with a comment explaining
   why the duplication is deliberate for a smoke test; for a dataset meant
   to be trusted for weeks it is the wrong trade -- if the join separator
   ever changes, a duplicated version quietly produces notes whose grounding
   never lines up, and staging would then be *worse* than empty because it
   would look fine.

   The script verifies this rather than asserting it: every seeded note is
   read back through `resolve_grounding` and the resolved citations are
   counted (`--verify`, on by default).

## The database this must never touch

Six locks, each independently sufficient, all checked before a single
INSERT:

  1. `settings.is_production` -- refuse outright.
  2. `ENVIRONMENT` must be one of `_ALLOWED_ENVIRONMENTS`. Fails **closed**:
     an unrecognised value is a refusal, not a shrug. A typo'd
     `ENVIRONMENT=prod-eu` must not read as "not production".
  3. `REMEDY_ALLOW_SYNTHETIC_SEED=1` must be set in the environment. A
     deliberate, per-shell opt-in that no `.env` in this repo contains.
  4. **Every clinician row must be on `@staging.remedy.example`.** This is
     the lock that actually makes production impossible rather than merely
     discouraged: a production database has real accounts in `clinicians`,
     so the script refuses before it can write. `.invalid` is reserved by
     RFC 2606 and can never resolve, so no real deploy can accidentally
     match it. Config can be wrong; the contents of a table cannot lie
     about whose database it is.
  5. The schema must be at the current Alembic head (reusing
     `scripts/check_migrations.py`), so a half-migrated target fails with a
     sentence instead of an `UndefinedColumn` traceback halfway through.
  6. `--yes`, or type the database name at a prompt.

And it **only ever INSERTs**. Not tidiness -- the consent ledger's
append-only trigger (P0-1) means a mis-seed physically cannot be cleaned
up, row by row, by anything including the table's owner. So there is no
`--reset`: the only real reset is dropping the database, the runbook says
so, and giving this script the privileges to do that itself would hand the
worst possible capability to the exact code path whose guards just failed.

Usage:
    export REMEDY_ALLOW_SYNTHETIC_SEED=1
    python scripts/seed_staging.py --yes
Full procedure and the reset path: docs/runbooks/staging.md
"""

from __future__ import annotations

import argparse
import io
import math
import os
import struct
import sys
import uuid
import wave
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# Runnable as `python scripts/seed_staging.py` from apps/api, which does not
# put apps/api itself on sys.path (only scripts/ goes there).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings, secret_fingerprint  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.models.clinician import Clinician  # noqa: E402
from app.models.consent import ConsentEventType, ConsentLedgerEntry  # noqa: E402
from app.models.encounter import Encounter, EncounterPipelineStatus  # noqa: E402
from app.models.note import Note, NoteRevision, NoteStatus  # noqa: E402
from app.models.patient import Patient  # noqa: E402
from app.services.asr.base import TranscriptSegment, TranscriptWord  # noqa: E402
from app.services.note_generation.base import GeneratedNote  # noqa: E402
from app.services.note_generation.shared import build_sections  # noqa: E402
from app.services.transcripts import persist_transcript  # noqa: E402

# --- the identity of synthetic data ---------------------------------------

#: RFC 2606 reserves `.invalid` permanently, so this domain can never be
#: registered, can never receive mail, and can never appear in a real
#: deploy by coincidence. That is what makes lock 4 above sound: the
#: presence of any *other* domain in `clinicians` is proof this is not a
#: staging database, and the absence of any row at all is proof it is
#: empty. Neither conclusion depends on a config value being correct.
SYNTHETIC_EMAIL_DOMAIN = "staging.remedy.example"
#
# `.example`, not `.invalid`, and the difference is load-bearing.
#
# Both are RFC 2606 reserved, so lock 4's argument is unchanged: neither
# will ever be a real clinic's domain, so their presence still proves this
# is not a production database. But `email-validator` -- which Pydantic's
# `EmailStr` uses, and which `LoginRequest.email` is typed as -- rejects
# `.invalid`, `.test` and `.localhost` outright as special-use TLDs. So
# every account seeded on `@staging.remedy.invalid` was refused at the
# login *schema*, with a 422, before any credential was ever checked.
#
# That is the same trap Phase 2.1 hit with a `@remedy.test` fixture, and it
# is worth naming: the property that made the domain safe (guaranteed never
# routable) is the property that made it unusable (guaranteed never
# deliverable). `.example` is reserved without being special-use, so it
# satisfies both.

#: Published on purpose, same reasoning as .env.example's PHI key
#: (decision 0031): a shared credential everyone already knows removes the
#: motive to reuse a real one, and it can be denied by value. The app's own
#: boot validator refuses published secrets in production; this password
#: never reaches that path because it is only ever hashed into a staging
#: `clinicians` row, which lock 4 then treats as a marker.
SYNTHETIC_PASSWORD = "staging-not-a-real-password"

#: A fixed, published TOTP secret for every seeded account, for the same
#: reason as the password above -- and to fix a bug this script had until it
#: was first used for onboarding.
#:
#: Login requires `mfa_secret is not None` AND a valid code (Phase 0.3).
#: This script previously created clinicians with **no MFA secret at all**,
#: so every seeded account was unloginnable: the dataset was complete,
#: realistic, verified end to end, and nobody could sign in to look at it.
#: `docs/runbooks/staging.md` even told the reader to "sign in as
#: doctor@staging.remedy.example" -- a documented step that could not
#: succeed. Nothing caught it because the API tests mint their own tokens
#: and never go through the login screen.
#:
#: Fixed and published rather than randomised per seed: a developer needs a
#: code in an authenticator app, and a per-run secret means re-enrolling on
#: every reseed. Valid base32, so `pyotp` accepts it.
SYNTHETIC_MFA_SECRET = "REMEDYSTAGINGSEEDMFA2222"

#: Fails closed. Anything not on this list -- including an empty string and
#: including `prod-eu`, `staging2`, or a typo -- is refused.
_ALLOWED_ENVIRONMENTS = frozenset({"development", "local", "staging", "test", "ci"})

_PROMPT_VERSION = "synthetic-staging-seed"


# --- patients: the fuzzy matcher's failure modes, by name -----------------
#
# Every name here is invented. They are shaped like Philippine names
# (given + given + mother's maiden + paternal surname, Spanish and
# Chinese-Filipino and Maguindanaon surnames, generational suffixes)
# because decision 0029's matcher is `difflib` over decrypted names and
# every one of its interesting cases is a naming-convention case.

@dataclass(frozen=True)
class PatientSpec:
    name: str
    birthdate: date
    #: What this row exists to exercise. Not decoration: a seed row whose
    #: purpose nobody recorded is the first one someone "simplifies" away.
    why: str


_PATIENTS: list[PatientSpec] = [
    PatientSpec(
        "Maria Concepcion Dela Cruz",
        date(1978, 3, 12),
        "Compound surname. A doctor types 'Maria Dela Cruz' -- one shared token and a "
        "sub-threshold whole-string ratio, which is exactly the case 0029's token "
        "prefilter was added to keep findable.",
    ),
    PatientSpec(
        "Maria Santos Cruz",
        date(1991, 11, 2),
        "Deliberate near-collision with the row above: 'Cruz' vs 'Dela Cruz', same "
        "given name. Search must return both and rank them; dedup must not link them.",
    ),
    PatientSpec(
        "Ma. Cristina Reyes-Bautista",
        date(1985, 7, 21),
        "The 'Ma.' abbreviation for Maria (ubiquitous on PH records) plus a hyphenated "
        "married surname. Tokenizing on whitespace gives 'ma.' and 'reyes-bautista', so "
        "a doctor typing 'Maria Bautista' shares NO token with this row -- the honest "
        "recall limit of the current matcher, kept visible instead of designed around.",
    ),
    PatientSpec(
        "Jose Antonio Bautista Jr.",
        date(1962, 1, 30),
        "Generational suffix; forms a father/son pair with the next row.",
    ),
    PatientSpec(
        "Jose Antonio Bautista",
        date(1990, 9, 8),
        "The other half of the pair. Names differ by 'Jr.' alone, so name similarity is "
        "~0.95 and ONLY the birthdate distinguishes them -- P0-6's 'dedup uses name + "
        "birthdate together, never name alone' in one pair of rows.",
    ),
    PatientSpec(
        "Juan Miguel Dela Cruz",
        date(2019, 5, 4),
        "Paediatric patient, shares a surname with patient 0 (a household). Also the "
        "only minor in the set, which is the consent flow's hardest case (P0-1: the "
        "roster records a guardian, not the patient).",
    ),
    PatientSpec(
        "Rosario Pena Villanueva",
        date(1955, 10, 17),
        "The n-with-tilde surname (Pena, dictated with a tilde) stored WITHOUT it, "
        "which is how it usually arrives from a keyboard with no easy way to type "
        "one. A doctor dictating it produces the accented form; difflib scores "
        "that pair at ~0.96 -- verified -- i.e. a near match needing one-tap "
        "confirmation, not a silent link. Stored unaccented on purpose so this row "
        "tests the matcher rather than the encoding -- and the tilde is described in "
        "words rather than typed, so this file stays pure ASCII. Phase 4.3 lost an "
        "afternoon to a cp1252 decode of one non-ASCII byte in a file a Windows tool "
        "read with the platform encoding; that lesson is cheap to keep.",
    ),
    PatientSpec(
        "Ana Marie Santos",
        date(1988, 2, 14),
        "Identical name to the next row, different birthdate. This is the case where "
        "P0-6's 'exact match links silently' MUST NOT fire (decision 0029), because "
        "silence here attaches a note to the wrong person. Nothing else in the "
        "directory reproduces it.",
    ),
    PatientSpec(
        "Ana Marie Santos",
        date(1996, 12, 1),
        "The second Ana Marie Santos. Deliberately a real duplicate name, not a typo.",
    ),
    PatientSpec(
        "Wilson Tan Sy",
        date(1971, 6, 25),
        "Chinese-Filipino naming: two short single-syllable surnames. Short tokens make "
        "difflib ratios jumpy (a two-character difference in a three-letter token is a "
        "large ratio move), which is where a threshold tuned on long Spanish surnames "
        "misbehaves.",
    ),
    PatientSpec(
        "Nur-Aisha Abdulkadir Mangudadatu",
        date(1994, 4, 19),
        "Maguindanaon naming, and the longest name in the set at 32 characters -- worth "
        "having one row that is nowhere near the EncryptedString(512) ceiling but is "
        "long enough that a truncating UI shows itself.",
    ),
    PatientSpec(
        "Juanito Ramos Aquino",
        date(1983, 4, 2),
        "The legal name of someone universally called 'Jun'. A doctor typing 'Jun "
        "Aquino' shares one token and scores ~0.6 -- above 0029's 0.55 search "
        "threshold, below its 0.82 dedup threshold. Precisely the band where 'search' "
        "and 'dedup' having different thresholds is what makes the flow work.",
    ),
    PatientSpec(
        "Ferdinand Emmanuel Salazar III",
        date(1948, 12, 5),
        "Roman-numeral suffix and the oldest patient in the set (77), so a "
        "date-of-birth widget defaulting to a recent decade is visibly wrong.",
    ),
    PatientSpec(
        "Precious Grace Ocampo",
        date(2001, 8, 30),
        "A given name of the kind common in Philippine records of the 1990s-2000s. "
        "Included so the directory is not uniformly Spanish-derived, which would make "
        "the token prefilter look better than it is.",
    ),
]


# --- consultations: Taglish, with the timings a grounding UI needs --------
#
# Invented consultations. Each turn is (speaker, line, start_ms). Words get
# uniform 220ms timings, which is not how speech works -- but it IS what
# Groq Whisper's word timings look like in shape (monotonic, non-
# overlapping, inside the recording's duration), and the grounding UI only
# ever consumes the shape.
#
# `low_confidence_words` names tokens that get a confidence BELOW
# settings.note_generation_low_confidence_threshold, so the [INAUDIBLE]
# suppression path (P0-4) appears in staging data rather than only in unit
# tests. Chosen to be clinically load-bearing words -- a dose, a duration --
# because that is when suppression matters.

@dataclass(frozen=True)
class ConsultSpec:
    key: str
    turns: list[tuple[str, str, int]]
    low_confidence_words: frozenset[str] = frozenset()
    #: (section, [(sentence, [turn indices cited])]). Turn INDICES, not
    #: "segN" strings: transcripts.py assigns ids by position, so citing by
    #: index cannot drift out of sync with the transcript the way a
    #: hand-written "seg4" can.
    sections: dict[str, list[tuple[str, list[int]]]] = field(default_factory=dict)
    #: Sections with nothing spoken behind them. Suppressed, not invented --
    #: the note generator's own rule (P0-4), and the reason a staging note
    #: should not have four tidy paragraphs.
    suppressed_sections: tuple[str, ...] = ()


_CONSULTS: list[ConsultSpec] = [
    ConsultSpec(
        key="lrti",
        turns=[
            ("speaker_0", "Magandang umaga po ano po ang naramdaman ninyo", 400),
            ("speaker_1", "Doktora apat na araw na akong nilalagnat at umuubo", 3_200),
            ("speaker_1", "Masakit ang dibdib ko kapag humihinga ako ng malalim", 6_100),
            ("speaker_0", "May plema ba at ano po ang kulay", 9_000),
            ("speaker_1", "Opo madilaw na plema at hirap akong matulog sa gabi", 11_200),
            ("speaker_0", "Ilang beses po kayo umiinom ng paracetamol araw araw", 14_600),
            ("speaker_1", "Tatlong beses po pero hindi nawawala ang lagnat", 17_800),
            ("speaker_0", "Kailangan nating i-x-ray ang dibdib at bibigyan kita ng antibiotic", 20_500),
            ("speaker_0", "Bumalik po kayo sa akin after three days para sa follow up", 24_200),
        ],
        # "Tatlong" is the dose frequency. Losing it is exactly the kind of
        # gap the doctor must see marked rather than smoothed over.
        low_confidence_words=frozenset({"Tatlong", "paracetamol"}),
        sections={
            "assessment": [
                ("Community-acquired pneumonia appears likely, pending chest imaging.", [1, 2, 4]),
                ("Patient reports four days of fever with productive cough.", [1, 4]),
            ],
            "plan": [
                ("Chest radiograph today.", [7]),
                ("Start empiric oral antibiotics.", [7]),
                ("Follow up in three days, or sooner if breathlessness worsens.", [8]),
            ],
            "subjective": [
                ('Patient reports "masakit ang dibdib ko kapag humihinga ako ng malalim".', [2]),
                ("Reports yellow sputum and difficulty sleeping at night.", [4]),
                (
                    "Antipyretic dose frequency was stated but not reliably transcribed; "
                    "confirm with the patient before prescribing.",
                    [6],
                ),
            ],
        },
        # Nothing was examined aloud on the recording.
        suppressed_sections=("objective",),
    ),
    ConsultSpec(
        key="hypertension",
        turns=[
            ("speaker_0", "Kumusta po kayo ngayon sa maintenance ninyo", 500),
            ("speaker_1", "Doktora nahihilo ako tuwing umaga tapos parang sumasakit ang batok ko", 3_400),
            ("speaker_0", "Umiinom pa po ba kayo ng amlodipine araw araw", 7_800),
            ("speaker_1", "Minsan po nakakalimutan ko kapag wala akong kasama sa bahay", 10_600),
            ("speaker_0", "Ang blood pressure ninyo ngayon ay one sixty over one hundred", 14_200),
            ("speaker_1", "Mataas po ba yun doktora natatakot ako sa stroke", 17_900),
            ("speaker_0", "Dagdagan natin ang dose at magpa-blood test po kayo para sa creatinine", 20_800),
            ("speaker_0", "Bawasan po ang maalat at mag-record kayo ng BP tuwing umaga", 25_100),
        ],
        low_confidence_words=frozenset({"creatinine"}),
        sections={
            "assessment": [
                ("Uncontrolled hypertension with reported medication non-adherence.", [1, 3, 4]),
                ("Occipital headache and morning dizziness reported.", [1]),
            ],
            "plan": [
                ("Increase antihypertensive dose.", [6]),
                ("Request renal function bloodwork; the specific test named was not reliably captured.", [6]),
                ("Advise sodium reduction and daily home blood-pressure logging.", [7]),
            ],
            "subjective": [
                ('Patient reports "nahihilo ako tuwing umaga".', [1]),
                ("Reports missing doses when unaccompanied at home.", [3]),
                ("Expresses fear of stroke.", [5]),
            ],
            "objective": [
                ("Blood pressure recorded at 160/100 mmHg during the consultation.", [4]),
            ],
        },
    ),
    ConsultSpec(
        key="dermatology",
        turns=[
            ("speaker_0", "Ano po ang problema sa balat ninyo", 300),
            ("speaker_1", "May makati pong pantal sa braso ko dalawang linggo na", 2_600),
            ("speaker_1", "Lumalaki po ito kapag napapawisan ako sa trabaho", 6_000),
            ("speaker_0", "Nagpahid po ba kayo ng kahit anong gamot o cream", 9_400),
            ("speaker_1", "Opo yung binili ko sa botika pero mas naging mapula", 12_100),
            ("speaker_0", "Tignan natin parang tinea corporis ito hindi allergy", 15_600),
            ("speaker_0", "Bibigyan kita ng antifungal cream dalawang beses araw araw for four weeks", 19_000),
        ],
        sections={
            "assessment": [
                ("Findings appear consistent with tinea corporis rather than contact allergy.", [5]),
                ("Two-week history of a pruritic lesion on the arm, worse with sweating.", [1, 2]),
            ],
            "plan": [
                ("Topical antifungal twice daily for four weeks.", [6]),
                ("Advise stopping the over-the-counter preparation currently in use.", [4]),
            ],
            "subjective": [
                ('Patient reports "may makati pong pantal sa braso ko dalawang linggo na".', [1]),
                ("Reports the lesion enlarges with sweating at work.", [2]),
                ("Reports increased redness after an over-the-counter cream.", [4]),
            ],
            "objective": [
                ("Lesion examined during the consultation.", [5]),
            ],
        },
    ),
    ConsultSpec(
        key="pediatric",
        turns=[
            ("speaker_0", "Sino po ang kasama ng bata ngayon", 300),
            ("speaker_1", "Ako po ang nanay niya doktora", 2_200),
            ("speaker_0", "Ano po ang nararamdaman ni Juan Miguel", 4_000),
            ("speaker_1", "Tatlong araw na po siyang may lagnat tapos ayaw kumain", 6_400),
            ("speaker_1", "Kagabi po nagsuka siya dalawang beses", 9_800),
            ("speaker_0", "May pagtatae po ba o pantal sa katawan", 12_400),
            ("speaker_1", "Wala pong pantal pero medyo malabnaw ang dumi niya", 15_200),
            ("speaker_0", "Kailangan natin ng CBC para ma-rule out ang dengue", 18_600),
            ("speaker_0", "Painumin po ninyo siya ng oral rehydration solution tuwing magtatae", 22_000),
        ],
        low_confidence_words=frozenset({"dalawang"}),
        sections={
            "assessment": [
                ("Acute febrile illness in a child; dengue not yet excluded.", [3, 7]),
                ("Three days of fever with reduced oral intake and loose stools.", [3, 6]),
            ],
            "plan": [
                ("Complete blood count to help exclude dengue.", [7]),
                ("Oral rehydration solution after each loose stool.", [8]),
            ],
            "subjective": [
                ("Mother reports three days of fever and refusal to eat.", [1, 3]),
                ("Reports vomiting overnight; the number of episodes was not reliably transcribed.", [4]),
                ("Reports no rash.", [6]),
            ],
            "objective": [
                ("History taken from the accompanying parent; the child was not examined on the recording.", [1]),
            ],
        },
    ),
]

_CONSULTS_BY_KEY = {c.key: c for c in _CONSULTS}


# --- encounters: the states a staging environment needs to be able to show -

@dataclass(frozen=True)
class EncounterSpec:
    label: str
    #: Index into _PATIENTS, or None for an unlinked "loose session" (P0-6:
    #: recording is never blocked on identity).
    patient: int | None
    consult: str | None
    consent: tuple[ConsentEventType, ...]
    pipeline: EncounterPipelineStatus
    note_status: NoteStatus | None = None
    #: "upload" puts real bytes in object storage; "expired" and "withdrawn"
    #: set a key and a deletion stamp WITHOUT touching storage, which is how
    #: the real thing looks after a lifecycle rule or a purge has run.
    audio: str = "none"
    #: (section, kind) with kind in {"cosmetic", "rewrite"}. Decision 0030
    #: reports two independent flags and they need two different edits to
    #: show up, so staging carries one of each.
    edits: tuple[tuple[str, str], ...] = ()
    last_error: str | None = None
    retry_count: int = 0
    why: str = ""


_ENCOUNTERS: list[EncounterSpec] = [
    EncounterSpec(
        label="signed, audio playable, cosmetically edited",
        patient=0,
        consult="lrti",
        consent=(ConsentEventType.GIVEN,),
        pipeline=EncounterPipelineStatus.NOTE_GENERATED,
        note_status=NoteStatus.SIGNED,
        audio="upload",
        edits=(("plan", "cosmetic"),),
        why="The happy path all the way to a signature, plus the subtle half of decision "
        "0030: a same-length edit leaves spans_fit True while edited_since_generation "
        "goes True. Nothing else in the dataset shows those two flags disagreeing.",
    ),
    EncounterSpec(
        label="authenticated, awaiting signature",
        patient=1,
        consult="hypertension",
        consent=(ConsentEventType.GIVEN,),
        pipeline=EncounterPipelineStatus.NOTE_GENERATED,
        note_status=NoteStatus.AUTHENTICATED,
        audio="upload",
        why="A note parked one step short of SIGNED, so the review screen's terminal "
        "transition can be exercised without re-seeding.",
    ),
    EncounterSpec(
        label="filed, four populated sections",
        patient=2,
        consult="dermatology",
        consent=(ConsentEventType.GIVEN,),
        pipeline=EncounterPipelineStatus.NOTE_GENERATED,
        note_status=NoteStatus.FILED,
        audio="upload",
        why="The only note with all four sections populated -- a useful contrast against "
        "the suppressed-Objective notes, which are the more common real shape.",
    ),
    EncounterSpec(
        label="generated, grounding withdrawn by a rewrite",
        patient=5,
        consult="pediatric",
        consent=(ConsentEventType.GIVEN,),
        pipeline=EncounterPipelineStatus.NOTE_GENERATED,
        note_status=NoteStatus.GENERATED,
        audio="upload",
        edits=(("assessment", "rewrite"),),
        why="Decision 0030's headline state: an insertion breaks the stored offsets, so "
        "spans_fit goes False and the UI must render plain text and say why. A staging "
        "set without this case cannot show the withheld-grounding path at all.",
    ),
    EncounterSpec(
        label="consent withdrawn, audio and transcript purged",
        patient=6,
        consult="hypertension",
        consent=(ConsentEventType.GIVEN, ConsentEventType.WITHDRAWN),
        pipeline=EncounterPipelineStatus.NOTE_GENERATED,
        note_status=NoteStatus.SIGNED,
        audio="withdrawn",
        why="P0-1 withdrawal after signing: the note stays in the record, the recording "
        "and transcript are gone, and grounding must say WITHDRAWN rather than EXPIRED "
        "(decision 0030 -- observably identical, legally not).",
    ),
    EncounterSpec(
        label="audio aged out under the retention rule",
        patient=9,
        consult="dermatology",
        consent=(ConsentEventType.GIVEN,),
        pipeline=EncounterPipelineStatus.NOTE_GENERATED,
        note_status=NoteStatus.SIGNED,
        audio="expired",
        why="The other end of the same ladder: EXPIRED, with the transcript still "
        "present. Together with the row above this is the only way to see that the two "
        "reasons render differently.",
    ),
    EncounterSpec(
        label="consent declined, pipeline terminal",
        patient=3,
        consult=None,
        consent=(ConsentEventType.DECLINED,),
        pipeline=EncounterPipelineStatus.BLOCKED_NO_CONSENT,
        why="Decision 0002: a consent violation is terminal, never retried. An encounter "
        "in this state with no transcript and no note is what that looks like in data.",
    ),
    EncounterSpec(
        label="loose session, no patient linked",
        patient=None,
        consult="lrti",
        consent=(ConsentEventType.GIVEN,),
        pipeline=EncounterPipelineStatus.NOTE_GENERATED,
        note_status=NoteStatus.GENERATED,
        audio="upload",
        why="P0-6: recording is never blocked on identity, so an encounter with a null "
        "patient_id is a legitimate state and the 'loose sessions' tray needs something "
        "in it. Also the reason this note cannot be FILED -- note_lifecycle refuses.",
    ),
    EncounterSpec(
        label="transcription failed after retries",
        patient=7,
        consult=None,
        consent=(ConsentEventType.GIVEN,),
        pipeline=EncounterPipelineStatus.TRANSCRIPTION_FAILED,
        audio="upload",
        last_error="ASR provider returned HTTP 503 after 3 attempts",
        retry_count=3,
        why="Phase 1.5 / decision 0023: a terminal per-stage failure that nothing is "
        "watching in real time. The error text is vendor/infrastructure only -- never "
        "transcript content -- which is why that column can never leak PHI.",
    ),
    EncounterSpec(
        label="transcribed, note generation failed",
        patient=8,
        consult="hypertension",
        consent=(ConsentEventType.GIVEN,),
        pipeline=EncounterPipelineStatus.GENERATION_FAILED,
        audio="upload",
        last_error="Note generator returned HTTP 429 after 3 attempts",
        retry_count=3,
        why="The other terminal stage, and the harder one to reason about: a transcript "
        "exists and is worth reading even though no note does.",
    ),
    EncounterSpec(
        label="generated, never recorded",
        patient=10,
        consult="pediatric",
        consent=(ConsentEventType.GIVEN,),
        pipeline=EncounterPipelineStatus.NOTE_GENERATED,
        note_status=NoteStatus.GENERATED,
        audio="none",
        why="AudioState.NEVER_RECORDED, which is not the same fact as a deletion and "
        "must not read as one. The third distinct rung of the audio ladder.",
    ),
]


# --- construction ---------------------------------------------------------


def _segments(consult: ConsultSpec, threshold: float) -> list[TranscriptSegment]:
    """One segment per turn, with per-word timings inside the turn.

    Confidence is 0.93 for ordinary words and `threshold - 0.2` for the ones
    named in `low_confidence_words`, so the suppression path is driven by the
    configured threshold rather than by a hardcoded number that would stop
    meaning anything if the setting changed.
    """
    out: list[TranscriptSegment] = []
    for speaker, line, start_ms in consult.turns:
        tokens = line.split()
        per = 220
        out.append(
            TranscriptSegment(
                speaker=speaker,
                words=[
                    TranscriptWord(
                        text=token,
                        start_ms=start_ms + i * per,
                        end_ms=start_ms + i * per + per - 20,
                        confidence=(threshold - 0.2 if token in consult.low_confidence_words else 0.93),
                        speaker=speaker,
                    )
                    for i, token in enumerate(tokens)
                ],
            )
        )
    return out


def _generated_note(consult: ConsultSpec, provider: str) -> GeneratedNote:
    """Builds the note through the PRODUCTION span builder.

    `build_sections` is what every real provider calls, so the offsets and
    the single-space join convention come from the same code Phase 3's
    `spans_fit_text` validates against. Re-deriving that convention here --
    as apps/web/smoke/seed_pipeline.py deliberately does for a smoke test --
    would mean a separator change silently produces staging notes whose
    grounding never fits, and a staging environment that looks right while
    being wrong is worse than an empty one.

    It also gives citation verification for free: `build_sections` drops any
    segment_id that is not in `valid_segment_ids`, so a typo in the specs
    above shows up as a sentence with no citations, which `--verify` counts.
    """
    valid_ids = {f"seg{i}" for i in range(len(consult.turns))}
    payload = {
        name: {
            "suppressed": name in consult.suppressed_sections,
            "sentences": [
                {"text": text, "segment_ids": [f"seg{i}" for i in turn_indices]}
                for text, turn_indices in consult.sections.get(name, [])
            ],
        }
        for name in ("assessment", "plan", "subjective", "objective")
    }
    sections = build_sections(payload, valid_ids)
    return GeneratedNote(
        assessment=sections["assessment"],
        plan=sections["plan"],
        subjective=sections["subjective"],
        objective=sections["objective"],
        provider=provider,
        prompt_version=_PROMPT_VERSION,
    )


def _synthetic_wav(duration_ms: int) -> bytes:
    """A short, deliberately non-silent WAV.

    Real audio, in the only sense that matters to everything downstream: the
    bytes exist in object storage, `head_object` finds them, a presigned
    Range request serves them, and a browser's `<audio>` element plays and
    seeks them. It is a decaying tone, not speech -- ASR would find nothing
    in it, which is correct, because staging must never contain a real voice
    (a recording of a real consultation is PHI regardless of which database
    the row lives in) and a synthesized voice would only invite someone to
    treat the transcript as ground truth.
    """
    rate = 16_000
    frames = int(rate * duration_ms / 1000)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        samples = bytearray()
        for n in range(frames):
            t = n / rate
            # Two tones plus a slow envelope, so a waveform display shows
            # visible structure instead of a solid bar.
            envelope = 0.35 * (1.0 + math.sin(2 * math.pi * 0.5 * t)) / 2
            value = envelope * (math.sin(2 * math.pi * 220 * t) + 0.5 * math.sin(2 * math.pi * 660 * t))
            samples += struct.pack("<h", int(max(-1.0, min(1.0, value)) * 32000))
        handle.writeframes(bytes(samples))
    return buffer.getvalue()


def _upload_audio(encounter_id: str, payload: bytes) -> str:
    """Puts the bytes in object storage over the real presigned-multipart
    path, not a direct boto3 `put_object`.

    One part, which S3 permits because the size floor applies to every part
    *but the last*. Using the production path means this seed exercises the
    same mechanics an uploading browser does (decision 0013) -- and if
    presigning is misconfigured in a staging environment, the seed is where
    that surfaces, rather than the first doctor to press record.
    """
    import httpx

    from app.services import storage

    key = storage.build_audio_object_key(encounter_id, "audio/wav")
    upload_id = storage.create_multipart_upload(key, "audio/wav")
    url = storage.presign_part_upload(key, upload_id, 1)
    response = httpx.put(url, content=payload, timeout=30.0)
    response.raise_for_status()
    storage.complete_multipart_upload(key, upload_id)
    return key


#: Same-length replacements, applied to produce the "cosmetic edit" state.
#: Same length is the whole point: decision 0030's `spans_fit_text` stays
#: True through an in-sentence substitution (the offsets genuinely do still
#: delimit that sentence) while `edited_since_generation` goes True, and
#: those two flags disagreeing is the case a staging set must be able to
#: show. Both pairs are clinically plausible edits, not filler -- a doctor
#: changing a follow-up interval is the commonest real edit there is.
_SAME_LENGTH_SWAPS = (("three", "seven"), ("Start", "Begin"), ("daily", "twice"))


def _cosmetic_edit(text: str) -> str:
    for find, replace in _SAME_LENGTH_SWAPS:
        if find in text:
            edited = text.replace(find, replace, 1)
            # A length change here would silently produce the OTHER state
            # (spans_fit False), and a seed that quietly seeds the wrong
            # scenario is worse than one that crashes.
            if len(edited) != len(text):
                raise RuntimeError(f"{find!r} -> {replace!r} changed the section length")
            return edited
    raise RuntimeError(f"no same-length swap applies to {text[:60]!r}; add one to _SAME_LENGTH_SWAPS")


def _rewrite_edit(text: str) -> str:
    """An insertion, which is what actually breaks stored offsets.

    Deliberately prepended: an insertion at the front shifts every
    subsequent span, so this is the maximally-broken case rather than a
    marginal one.
    """
    return "Reviewed and rewritten by the attending physician. " + text


# --- guards ---------------------------------------------------------------


def _redact(url: str) -> str:
    """Database URL with the password removed, safe to print into a terminal
    someone may screenshot into a ticket.
    """
    if "@" not in url:
        return url
    scheme, rest = url.split("://", 1) if "://" in url else ("", url)
    creds, host = rest.rsplit("@", 1)
    user = creds.split(":", 1)[0]
    return f"{scheme}://{user}:***@{host}" if scheme else f"{user}:***@{host}"


def preflight(db) -> list[str]:
    """Every reason this database must not be seeded, collected together.

    All problems at once rather than one per run: a target that fails one of
    these usually fails several, and finding that out one restart at a time
    is what makes someone start disabling them.
    """
    from sqlalchemy import text as sql_text

    from scripts.check_migrations import check_single_head

    settings = get_settings()
    problems: list[str] = []

    # Locks 1 and 2. The allow-list is checked *as well as* is_production,
    # not instead of it: is_production only recognises "production"/"prod",
    # so ENVIRONMENT=prod-eu would slip past it -- and the allow-list
    # catches exactly that by refusing everything it does not recognise.
    if settings.is_production:
        problems.append(f"ENVIRONMENT={settings.environment!r} is a production environment. This script writes rows.")
    elif settings.environment.strip().lower() not in _ALLOWED_ENVIRONMENTS:
        problems.append(
            f"ENVIRONMENT={settings.environment!r} is not one of {sorted(_ALLOWED_ENVIRONMENTS)}. "
            "This check fails closed on purpose: an unrecognised environment name is refused "
            "rather than assumed safe."
        )

    # Lock 3.
    if os.environ.get("REMEDY_ALLOW_SYNTHETIC_SEED") != "1":
        problems.append(
            "REMEDY_ALLOW_SYNTHETIC_SEED is not set to 1. Set it in the shell you run this "
            "from; it is deliberately absent from every .env in this repository, so no "
            "process can inherit an opt-in it did not make."
        )

    # Lock 5, and it has to come BEFORE lock 4 rather than after: lock 4
    # reads the `clinicians` table, which does not exist on an unmigrated
    # database. The first draft had these the other way round and the
    # refusal arrived as a 90-line UndefinedTable traceback -- technically a
    # refusal, but the kind that reads as a broken script rather than a
    # working guard, which is how guards get worked around.
    try:
        applied = db.execute(sql_text("SELECT version_num FROM alembic_version")).scalar()
    except Exception:  # noqa: BLE001 - a missing table is the interesting case, not the exception type
        db.rollback()
        applied = None
    head = check_single_head()[0]
    if applied != head:
        problems.append(
            f"Schema is at revision {applied!r}, not head {head!r}. Run `alembic upgrade head` "
            "first -- a half-migrated target otherwise fails partway through with an "
            "UndefinedColumn traceback instead of a sentence."
        )
        # Return early: every check below queries a table, and reporting
        # "clinicians does not exist" alongside "run your migrations" adds
        # noise to an already-answered question.
        return problems

    # Lock 4 -- the one that does not depend on configuration being right.
    foreign = [c.email for c in db.query(Clinician).all() if not c.email.endswith("@" + SYNTHETIC_EMAIL_DOMAIN)]
    if foreign:
        problems.append(
            f"{len(foreign)} clinician account(s) are not on @{SYNTHETIC_EMAIL_DOMAIN} "
            f"(e.g. {foreign[0]!r}). This database holds real accounts, so it is not a "
            "staging database. Refusing before writing anything."
        )

    already = db.query(Clinician).filter(Clinician.email.like("%@" + SYNTHETIC_EMAIL_DOMAIN)).count()
    if already:
        problems.append(
            f"This database is already seeded ({already} synthetic clinician(s)). Running "
            "again would append duplicates, and there is no --reset: the consent ledger's "
            "append-only trigger (P0-1) means seeded rows cannot be deleted by anything, "
            "the table owner included. Drop and recreate the database instead -- "
            "docs/runbooks/staging.md has the two commands."
        )

    return problems


# --- the seed itself ------------------------------------------------------


@dataclass
class Counts:
    clinicians: int = 0
    patients: int = 0
    encounters: int = 0
    consent_entries: int = 0
    transcripts: int = 0
    notes: int = 0
    revisions: int = 0
    audio_objects: int = 0


def seed(db, *, with_audio: bool) -> Counts:
    """Inserts the whole dataset. INSERT only -- see the module docstring."""
    from app.services import note_lifecycle

    settings = get_settings()
    counts = Counts()
    now = datetime.now(timezone.utc)

    # Three roles, because RBAC (P0-8, decision 0005) is need-to-know and a
    # single-account staging environment cannot show a compliance officer
    # being refused a note they have no reason to read.
    hashed = hash_password(SYNTHETIC_PASSWORD)
    clinicians = {
        "doctor": Clinician(
            email="doctor@" + SYNTHETIC_EMAIL_DOMAIN,
            full_name="Dr. Lourdes Katigbak Arellano",
            hashed_password=hashed,
            # Without this the account cannot log in at all -- see
            # SYNTHETIC_MFA_SECRET for the bug this fixes.
            mfa_secret=SYNTHETIC_MFA_SECRET,
            role="doctor",
            # Signing requires one (P0-5). Format-shaped and obviously fake:
            # PRC numbers are seven digits, and a plausible-looking real one
            # on a staging account is a small, entirely avoidable way to
            # impersonate an actual physician.
            prc_license_number="0000001",
        ),
        "compliance": Clinician(
            email="compliance@" + SYNTHETIC_EMAIL_DOMAIN,
            full_name="Corazon Bautista Lim",
            hashed_password=hashed,
            mfa_secret=SYNTHETIC_MFA_SECRET,
            role="compliance",
        ),
        "admin": Clinician(
            email="admin@" + SYNTHETIC_EMAIL_DOMAIN,
            full_name="Ramon Delfin Espiritu",
            hashed_password=hashed,
            mfa_secret=SYNTHETIC_MFA_SECRET,
            role="admin",
        ),
    }
    db.add_all(list(clinicians.values()))
    db.commit()
    counts.clinicians = len(clinicians)
    doctor = clinicians["doctor"]

    patients = [Patient(full_name=spec.name, birthdate=spec.birthdate) for spec in _PATIENTS]
    db.add_all(patients)
    db.commit()
    counts.patients = len(patients)

    audio_bytes = b""
    if with_audio:
        from app.services import storage

        # Once, before the loop, and the same call the API makes at startup.
        # A fresh staging stack therefore needs no manual `mc mb` step -- and,
        # more usefully, this installs the retention lifecycle rule
        # (decisions 0014 and 0033) so a staging bucket expires audio on the
        # same schedule production does instead of keeping it forever.
        # MinIO logs a NotImplemented warning for the encryption call; that is
        # decision 0014's documented MinIO gap, not a failure here.
        storage.ensure_bucket_configured()
        audio_bytes = _synthetic_wav(28_000)

    for order, spec in enumerate(_ENCOUNTERS):
        encounter = Encounter(
            clinician_id=doctor.id,
            patient_id=patients[spec.patient].id if spec.patient is not None else None,
            # A staging-recognisable idempotency key. Unique per row, and
            # readable in a psql session, which matters when the question is
            # "which of these eleven encounters am I looking at".
            upload_idempotency_key=f"staging-{order:02d}-{uuid.uuid4().hex[:8]}",
            pipeline_status=spec.pipeline,
            retry_count=spec.retry_count,
            last_pipeline_error=spec.last_error,
            # Spread across a fortnight so any "recent activity" ordering has
            # something to order by. A dataset where every row shares a
            # timestamp makes a sorting bug invisible.
            pipeline_updated_at=now - timedelta(days=len(_ENCOUNTERS) - order, hours=order),
        )
        db.add(encounter)
        db.commit()
        db.refresh(encounter)
        counts.encounters += 1

        for event in spec.consent:
            db.add(
                ConsentLedgerEntry(
                    encounter_id=encounter.id,
                    event=event,
                    # A roster naming roles, not just a count -- P0-1 asks
                    # for the participants. The paediatric encounter carries
                    # a guardian instead of the patient, which is the case
                    # that makes the roster a list rather than a boolean.
                    participant_roster=(
                        '["clinician", "guardian", "patient (minor)"]'
                        if spec.consult == "pediatric"
                        else '["clinician", "patient"]'
                    ),
                    purposes='["clinical documentation", "quality review"]',
                    # Filipino, because the point of the script being in the
                    # language of the room is that the ledger records which
                    # script was actually read (P0-1).
                    script_language="fil",
                )
            )
            counts.consent_entries += 1
        db.commit()

        # Under --no-audio an "upload" scenario becomes a never-recorded
        # encounter, deliberately: it does NOT get a key pointing at bytes
        # that were never written.
        #
        # The first draft did write one, and rehearsing the CI job caught
        # what that costs. `grounding._audio_state` treats "the row claims a
        # key, storage says 404" as proof the lifecycle rule expired the
        # object, and stamps `audio_deleted_at` accordingly -- correctly,
        # because in production that inference is sound. Handing it a key
        # for an upload that never happened makes it record a retention
        # expiry that never occurred: exactly decision 0030's confidently-
        # wrong answer, manufactured by the seed itself.
        if spec.audio in ("expired", "withdrawn") or (spec.audio == "upload" and with_audio):
            if spec.audio == "upload":
                encounter.audio_object_key = _upload_audio(encounter.id, audio_bytes)
                counts.audio_objects += 1
            else:
                # A key with no object behind it, which is legitimate here
                # and only here: it is exactly what the database looks like
                # after a lifecycle rule or a purge has run, and it is
                # paired with the audio_deleted_at stamp below, so nothing
                # ever offers a play button for bytes that are not there.
                encounter.audio_object_key = f"encounters/{encounter.id}/audio/{uuid.uuid4().hex}.wav"
            encounter.audio_retention_expires_at = now + timedelta(days=settings.audio_retention_days)
            if spec.audio in ("expired", "withdrawn"):
                encounter.audio_deleted_at = now - timedelta(days=1)
            db.add(encounter)
            db.commit()

        consult = _CONSULTS_BY_KEY[spec.consult] if spec.consult else None
        if consult is None:
            continue

        # The withdrawn encounter's transcript is gone, removed by the same
        # purge that removed its audio (Phase 4.4). Its note stays -- that is
        # the point of the state, and TranscriptState.WITHDRAWN is what the
        # grounding UI must report for it.
        if spec.audio != "withdrawn":
            persist_transcript(
                db,
                encounter.id,
                provider_name="synthetic-staging-seed",
                model_version="n/a",
                segments=_segments(consult, settings.note_generation_low_confidence_threshold),
            )
            counts.transcripts += 1

        if spec.note_status is None:
            continue

        generated = _generated_note(consult, settings.note_generator_provider)
        note = Note(
            encounter_id=encounter.id,
            assessment=generated.assessment.text,
            plan=generated.plan.text,
            subjective=generated.subjective.text,
            objective=generated.objective.text,
            source_spans=generated.source_spans_json(),
            note_generator_provider=generated.provider,
            prompt_version=generated.prompt_version,
        )
        db.add(note)
        db.commit()
        db.refresh(note)
        counts.notes += 1

        for section, kind in spec.edits:
            previous = getattr(note, section)
            new = _cosmetic_edit(previous) if kind == "cosmetic" else _rewrite_edit(previous)
            db.add(
                NoteRevision(
                    note_id=note.id,
                    section=section,
                    previous_text=previous,
                    new_text=new,
                    edited_by_clinician_id=doctor.id,
                )
            )
            setattr(note, section, new)
            db.add(note)
            db.commit()
            counts.revisions += 1

        # Driven through note_lifecycle, never by assigning note.status. That
        # module is the only code allowed to write the column (P0-5), so
        # seeding around it would produce a dataset the application itself
        # could not have created -- which is the specific way a seed script
        # stops being a rehearsal and becomes a fiction.
        for target in (NoteStatus.FILED, NoteStatus.AUTHENTICATED, NoteStatus.SIGNED):
            if note.status == spec.note_status:
                break
            note_lifecycle.transition(
                db,
                note,
                target,
                clinician_id=doctor.id,
                prc_license_number=doctor.prc_license_number,
                confirmed_patient_id=encounter.patient_id,
            )

    return counts


# --- verification ---------------------------------------------------------


def verify(db) -> tuple[list[str], list[str]]:
    """Reads every seeded note back through `resolve_grounding` and reports
    what a doctor would actually see.

    This is what makes the dataset trustworthy rather than merely present.
    "Notes with citations" is easy to claim and easy to get wrong: a citation
    resolves only if the segment ids in `source_spans` match ids the
    transcript actually assigned AND the stored offsets still fit the note's
    current text. Counting resolved segments proves both, through the same
    read path the UI uses.

    Returns (report lines, problems).
    """
    from app.services.grounding import resolve_grounding

    lines: list[str] = []
    problems: list[str] = []

    for note in db.query(Note).all():
        grounding = resolve_grounding(db, note)
        cited = sum(1 for s in grounding.segments if s.cited)
        context = len(grounding.segments) - cited
        fitting = [name for name, s in grounding.sections.items() if s.spans_fit]
        edited = [name for name, s in grounding.sections.items() if s.edited_since_generation]
        suppressed = [name for name, s in grounding.sections.items() if s.suppressed]
        lines.append(
            f"  note {note.id[:8]} status={note.status.value:14s} "
            f"audio={grounding.audio_state.value:13s} transcript={grounding.transcript_state.value:16s} "
            f"cited={cited:2d} (+{context} ctx) fit={sorted(fitting)} edited={sorted(edited)} "
            f"suppressed={sorted(suppressed)}"
        )

        # A note with a live transcript and no resolvable citation is the
        # failure this whole function exists to catch: it means the seed
        # produced notes whose grounding panel is empty, which looks like a
        # working staging environment and is not one.
        if grounding.transcript_state.value == "available" and cited == 0:
            problems.append(f"note {note.id} has a live transcript but resolved ZERO cited segments")

    return lines, problems


def _print_sign_in_details() -> None:
    """Print credentials that actually work, including a live TOTP code.

    Printed by the script rather than left to a runbook, because a runbook
    drifts: this script created accounts with no MFA secret while
    docs/runbooks/staging.md told the reader to sign in as the doctor, and
    nothing reconciled the two. Output from the script that created the
    accounts cannot disagree with the accounts it created.
    """
    try:
        import pyotp

        code = pyotp.TOTP(SYNTHETIC_MFA_SECRET).now()
        code_line = f"{code}  (valid ~30s -- rerun if rejected)"
    except Exception:  # noqa: BLE001 - a missing code must never fail the seed
        code_line = "(unavailable; put the secret in an authenticator app)"

    print(
        "\nSign in at the web client with any of:"
        f"\n  doctor@{SYNTHETIC_EMAIL_DOMAIN}      records, reviews and signs"
        f"\n  compliance@{SYNTHETIC_EMAIL_DOMAIN}  read and audit only"
        f"\n  admin@{SYNTHETIC_EMAIL_DOMAIN}       system role"
        f"\n\n  password    {SYNTHETIC_PASSWORD}"
        f"\n  MFA secret  {SYNTHETIC_MFA_SECRET}"
        f"\n  MFA code    {code_line}"
        "\n\nPrint a fresh code any time with:"
        "\n  python -c \'import pyotp; print(pyotp.TOTP(\\'REMEDYSTAGINGSEEDMFA2222\\').now())\'"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Seed a staging database with synthetic (never real) clinical data.",
        epilog="See docs/runbooks/staging.md. Requires REMEDY_ALLOW_SYNTHETIC_SEED=1.",
    )
    parser.add_argument("--yes", action="store_true", help="Skip the interactive confirmation.")
    parser.add_argument(
        "--no-audio",
        action="store_true",
        help=(
            "Skip the object-storage uploads. Encounters that would have carried a "
            "recording are seeded as never-recorded instead of pointing at bytes that "
            "do not exist -- see the comment in seed(). The retention-expired and "
            "consent-withdrawn scenarios are unaffected; they never needed an object."
        ),
    )
    parser.add_argument("--no-verify", action="store_true", help="Skip the grounding read-back.")
    args = parser.parse_args(argv)

    settings = get_settings()
    print("Target:")
    print(f"  DATABASE_URL   {_redact(settings.database_url)}")
    print(f"  ENVIRONMENT    {settings.environment}")
    print(f"  S3_ENDPOINT    {settings.s3_endpoint_url} (bucket {settings.s3_bucket})")
    # The fingerprint, never the key (4.1's secret_fingerprint). Enough to
    # answer "is staging using the same key as production" -- the question
    # that actually matters -- without writing either key down.
    print(
        "  PHI key        "
        + (secret_fingerprint(settings.phi_encryption_key) if settings.phi_encryption_key else "(unset)")
    )

    db = SessionLocal()
    try:
        problems = preflight(db)
        if problems:
            print("\nRefusing to seed this database:\n  - " + "\n  - ".join(problems), file=sys.stderr)
            return 1

        if not args.yes:
            target = settings.database_url.rsplit("/", 1)[-1]
            typed = input(f"\nType the database name to confirm ({target}): ").strip()
            if typed != target:
                print("Aborted.", file=sys.stderr)
                return 1

        counts = seed(db, with_audio=not args.no_audio)
        print("\nSeeded:")
        for field_name, value in vars(counts).items():
            print(f"  {field_name:16s} {value}")

        if args.no_verify:
            print("\nSkipped verification (--no-verify). The dataset is unproven.")
            return 0

        lines, verify_problems = verify(db)
        print("\nGrounding read-back:")
        print("\n".join(lines))
        if verify_problems:
            print("\nVERIFICATION FAILED:\n  - " + "\n  - ".join(verify_problems), file=sys.stderr)
            return 1
        print("\nOK: every seeded note with a live transcript resolves at least one cited segment.")
        _print_sign_in_details()
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
