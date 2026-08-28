"""Phase 4.1: PHI key rotation, and the guards around key material.

The checklist's ⚠️ for 4.1 is that PHI_ENCRYPTION_KEY is a single point of
catastrophe with no rotation story. These tests are the rotation story:
they assert the properties that make losing or replacing a key survivable,
including the two that fail silently if nobody checks them —

  * that the rotation script finds EVERY encrypted column (a missed column
    is data permanently unreadable once the old key is deleted), and
  * that a rotated value no longer reads under the old key alone, which is
    the only evidence the rewrite actually happened.

Procedure and rehearsal timings: docs/runbooks/key-rotation.md.
Why app-layer Fernet at all: docs/decisions/0031.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from datetime import date
from pathlib import Path

import pytest
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import text

from app.core.config import Settings
from app.core.security import (
    EncryptedJSON,
    EncryptedString,
    build_phi_cipher,
    phi_cipher,
    reset_phi_cipher_cache,
)
from app.models.clinician import Clinician
from app.models.encounter import Encounter
from app.models.note import Note, NoteRevision
from app.models.patient import Patient
from app.models.transcript import Transcript
from scripts.rotate_phi_key import encrypted_columns, main

_API_ROOT = Path(__file__).resolve().parents[1]

# The key tests/conftest.py generated for this process. Everything already in
# the database is encrypted under it, so rotations here start from it.
CURRENT_KEY = os.environ["PHI_ENCRYPTION_KEY"]
NEW_KEY = Fernet.generate_key().decode()


@pytest.fixture(autouse=True)
def _restore_process_cipher():
    """Any test that swaps the settings singleton's keys must put the cached
    cipher back, or every later test in this process silently decrypts with
    the wrong key set.
    """
    yield
    reset_phi_cipher_cache()


# --- What the rotation has to cover -------------------------------------


def test_every_encrypted_column_in_the_schema_is_discovered():
    """The single most dangerous bug this script could have is missing a
    column. It is asserted as an exact set, not a subset: adding a new
    EncryptedString column should fail this test loudly and make whoever
    added it confirm the rotation covers it, which is much cheaper than
    finding out after an old key has been destroyed.
    """
    found = {t.name: sorted(cols) for t, cols in encrypted_columns().items()}
    assert found == {
        "patients": ["full_name"],
        "notes": ["assessment", "objective", "plan", "subjective"],
        "note_revisions": ["new_text", "previous_text"],
        "transcripts": ["segments"],
    }


def test_discovery_is_by_column_type_rather_than_a_hand_written_list():
    """Every discovered column really is one of ours — the guarantee that
    makes the exact-set assertion above meaningful rather than a
    transcription of a list somebody typed twice.
    """
    for table, columns in encrypted_columns().items():
        for name in columns:
            assert isinstance(table.columns[name].type, (EncryptedString, EncryptedJSON))


# --- MultiFernet's rotation properties, verified rather than assumed -----


def test_multifernet_encrypts_with_the_first_key_and_decrypts_with_any():
    old, new = Fernet(CURRENT_KEY.encode()), Fernet(NEW_KEY.encode())
    cipher = build_phi_cipher(NEW_KEY, [CURRENT_KEY])

    written_under_old = old.encrypt(b"Maria Santos Dela Cruz")
    assert cipher.decrypt(written_under_old) == b"Maria Santos Dela Cruz"

    fresh = cipher.encrypt(b"Maria Santos Dela Cruz")
    assert new.decrypt(fresh) == b"Maria Santos Dela Cruz"
    with pytest.raises(InvalidToken):
        old.decrypt(fresh)


def test_a_rotated_token_stops_reading_under_the_old_key():
    """The property the whole procedure rests on. If this were false,
    "rotation complete" would be unfalsifiable and the old key could never
    safely be destroyed.
    """
    old = Fernet(CURRENT_KEY.encode())
    rotated = build_phi_cipher(NEW_KEY, [CURRENT_KEY]).rotate(old.encrypt(b"epigastric pain"))

    assert Fernet(NEW_KEY.encode()).decrypt(rotated) == b"epigastric pain"
    with pytest.raises(InvalidToken):
        old.decrypt(rotated)


def test_the_app_reads_rows_written_under_a_previous_key(monkeypatch):
    """The zero-downtime half: during a rotation the database holds a mix of
    both, and the running app has to serve every patient regardless of which
    key their row happens to be under.
    """
    settings = Settings(
        phi_encryption_key=NEW_KEY,
        phi_encryption_key_previous=CURRENT_KEY,
    )
    monkeypatch.setattr("app.core.security.get_settings", lambda: settings)
    reset_phi_cipher_cache()

    written_before_rotation = Fernet(CURRENT_KEY.encode()).encrypt(b"Juan Reyes")
    assert phi_cipher().decrypt(written_before_rotation) == b"Juan Reyes"
    # ...and new writes go out under the new key, not the old one.
    assert Fernet(NEW_KEY.encode()).decrypt(phi_cipher().encrypt(b"Juan Reyes")) == b"Juan Reyes"


# --- The script, end to end against a real database ---------------------


def _seed_one_of_everything(db) -> dict[str, str]:
    """One row in each table holding an encrypted column, with the plaintext
    returned so the test can prove rotation preserved it exactly.
    """
    clinician = Clinician(email="doc@example.com", full_name="Dr. Reyes", hashed_password="x")
    patient = Patient(full_name="Maria Santos Dela Cruz", birthdate=date(1978, 4, 11))
    db.add_all([clinician, patient])
    db.flush()

    encounter = Encounter(
        patient_id=patient.id,
        clinician_id=clinician.id,
        upload_idempotency_key=str(uuid.uuid4()),
    )
    db.add(encounter)
    db.flush()

    note = Note(
        encounter_id=encounter.id,
        assessment="Likely peptic ulcer disease.",
        plan="Omeprazole 20mg BID for 14 days.",
        subjective="Three days of epigastric pain.",
        objective="Epigastric tenderness, no guarding.",
        note_generator_provider="groq",
    )
    db.add(note)
    db.flush()

    revision = NoteRevision(
        note_id=note.id,
        section="assessment",
        previous_text="Gastritis.",
        new_text="Likely peptic ulcer disease.",
        edited_by_clinician_id=clinician.id,
    )
    transcript = Transcript(
        encounter_id=encounter.id,
        asr_provider="groq",
        asr_model_version="whisper-large-v3",
        segments=[{"id": "seg0", "speaker": "speaker_0", "words": [{"text": "pain"}]}],
    )
    db.add_all([revision, transcript])
    db.commit()
    return {"patient": patient.id, "note": note.id, "revision": revision.id, "transcript": transcript.id}


def _raw(db, table: str, column: str, row_id: str) -> str:
    """Ciphertext straight from the column, with no TypeDecorator in the way."""
    return db.execute(
        text(f"SELECT {column} FROM {table} WHERE id = :id"),  # noqa: S608 - literal identifiers
        {"id": row_id},
    ).scalar_one()


ALL_VALUES = [
    ("patients", "full_name", "patient"),
    ("notes", "assessment", "note"),
    ("notes", "plan", "note"),
    ("notes", "subjective", "note"),
    ("notes", "objective", "note"),
    ("note_revisions", "previous_text", "revision"),
    ("note_revisions", "new_text", "revision"),
    ("transcripts", "segments", "transcript"),
]


def test_rotation_rewrites_every_encrypted_value_and_preserves_the_plaintext(db, capsys):
    ids = _seed_one_of_everything(db)
    before = {(t, c): _raw(db, t, c, ids[k]) for t, c, k in ALL_VALUES}

    # batch_size=1 so the keyset pager runs several times rather than
    # swallowing the whole table in one query — paging is where a rotation
    # silently skips rows.
    assert main(["--new-key", NEW_KEY, "--old-key", CURRENT_KEY, "--batch-size", "1"]) == 0

    old, new = Fernet(CURRENT_KEY.encode()), Fernet(NEW_KEY.encode())
    for t, c, k in ALL_VALUES:
        after = _raw(db, t, c, ids[k])
        assert after != before[(t, c)], f"{t}.{c} was not rewritten"
        # Readable under the new key alone, and no longer under the old.
        plaintext = new.decrypt(after.encode())
        with pytest.raises(InvalidToken):
            old.decrypt(after.encode())
        # And byte-identical to what was there before, which is the whole
        # point — a rotation that mangles PHI is worse than none.
        assert plaintext == old.decrypt(before[(t, c)].encode())

    assert json.loads(new.decrypt(_raw(db, "transcripts", "segments", ids["transcript"]).encode()))[0]["id"] == "seg0"

    # The operator is told what to do next, not left to guess.
    assert "PHI_ENCRYPTION_KEY_PREVIOUS" in capsys.readouterr().out


def test_verify_only_passes_under_the_new_key_and_fails_under_the_old(db):
    _seed_one_of_everything(db)
    assert main(["--new-key", NEW_KEY, "--old-key", CURRENT_KEY, "--batch-size", "2"]) == 0

    assert main(["--new-key", NEW_KEY, "--verify-only"]) == 0
    # The negative case matters more: verification has to be capable of
    # failing, or a green run means nothing.
    assert main(["--new-key", CURRENT_KEY, "--verify-only"]) == 1


def test_verify_only_catches_a_row_the_rotation_never_reached(db):
    """The exact failure an OFFSET-paged or partially-completed rotation
    leaves behind: most rows moved, one did not.
    """
    ids = _seed_one_of_everything(db)
    assert main(["--new-key", NEW_KEY, "--old-key", CURRENT_KEY]) == 0

    stale = Fernet(CURRENT_KEY.encode()).encrypt(b"Left behind").decode()
    db.execute(text("UPDATE patients SET full_name = :v WHERE id = :id"), {"v": stale, "id": ids["patient"]})
    db.commit()

    assert main(["--new-key", NEW_KEY, "--verify-only"]) == 1


def test_rotation_is_idempotent_so_an_interrupted_run_can_just_be_re_run(db):
    ids = _seed_one_of_everything(db)
    assert main(["--new-key", NEW_KEY, "--old-key", CURRENT_KEY]) == 0
    assert main(["--new-key", NEW_KEY, "--old-key", CURRENT_KEY]) == 0
    assert main(["--new-key", NEW_KEY, "--verify-only"]) == 0
    assert (
        Fernet(NEW_KEY.encode()).decrypt(_raw(db, "patients", "full_name", ids["patient"]).encode())
        == b"Maria Santos Dela Cruz"
    )


def test_dry_run_reads_everything_and_writes_nothing(db):
    ids = _seed_one_of_everything(db)
    before = _raw(db, "patients", "full_name", ids["patient"])

    assert main(["--new-key", NEW_KEY, "--old-key", CURRENT_KEY, "--dry-run"]) == 0

    assert _raw(db, "patients", "full_name", ids["patient"]) == before


def test_a_value_no_key_can_decrypt_fails_the_run_rather_than_being_skipped(db, capsys):
    """A run that shrugs at an undecryptable value and exits 0 is how an old
    key gets deleted while data still depends on it.
    """
    ids = _seed_one_of_everything(db)
    orphan = Fernet(Fernet.generate_key()).encrypt(b"encrypted under a third key").decode()
    db.execute(text("UPDATE patients SET full_name = :v WHERE id = :id"), {"v": orphan, "id": ids["patient"]})
    db.commit()

    assert main(["--new-key", NEW_KEY, "--old-key", CURRENT_KEY]) == 1
    assert "patients.full_name" in capsys.readouterr().err


def test_the_run_never_prints_key_material(db, capsys):
    """This output goes into a change record. Fingerprints, not keys."""
    _seed_one_of_everything(db)
    main(["--new-key", NEW_KEY, "--old-key", CURRENT_KEY])

    captured = capsys.readouterr()
    assert NEW_KEY not in captured.out + captured.err
    assert CURRENT_KEY not in captured.out + captured.err


# --- Key material is refused before it can be used ----------------------


def _boot(tmp_path: Path, **env: str) -> subprocess.CompletedProcess:
    """Import the app in a clean subprocess, so the guard is exercised the
    way a deploy exercises it: at process start, before any request.

    cwd is a temp directory rather than apps/api because Settings reads a
    relative `.env`, and a developer's own .env would otherwise decide the
    outcome of these assertions.
    """
    return subprocess.run(
        [sys.executable, "-c", "import app.main; print('BOOTED', app.main.app.docs_url)"],
        cwd=tmp_path,
        env={
            "PATH": os.environ.get("PATH", ""),
            "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
            "PYTHONPATH": str(_API_ROOT),
            "S3_PROVISION_BUCKET_ON_STARTUP": "false",
            **env,
        },
        capture_output=True,
        text=True,
    )


PUBLISHED_DEV_KEY = "jy8XzNfhCgDamDDLM0DAGmlpYmLKFQNpt6XLt402fyw="
PRODUCTION_ENV = {
    "ENVIRONMENT": "production",
    "REFRESH_COOKIE_SECURE": "true",
    "CORS_ALLOW_ORIGINS": "https://scribe.remedy.example",
    "JWT_SECRET": "a-real-production-jwt-secret-value",
    "S3_SECRET_KEY": "a-real-production-object-store-secret",
}


def test_a_development_process_starts_with_the_published_key(tmp_path):
    """The other half of publishing the dev key: it has to actually work,
    or developers will go looking for a real one.
    """
    result = _boot(tmp_path, ENVIRONMENT="development", PHI_ENCRYPTION_KEY=PUBLISHED_DEV_KEY)
    assert result.returncode == 0, result.stderr
    assert "BOOTED /docs" in result.stdout


def test_production_refuses_to_start_with_the_published_development_key(tmp_path):
    result = _boot(tmp_path, **PRODUCTION_ENV, PHI_ENCRYPTION_KEY=PUBLISHED_DEV_KEY)
    assert result.returncode != 0
    assert "published in this repository" in result.stderr
    assert "PHI_ENCRYPTION_KEY" in result.stderr


def test_production_refuses_a_repository_key_hiding_in_the_previous_key_list(tmp_path):
    """A rotation *away* from the published key is exactly when someone
    would paste it into PHI_ENCRYPTION_KEY_PREVIOUS — where it would keep
    working, and keep being public.
    """
    result = _boot(
        tmp_path,
        **PRODUCTION_ENV,
        PHI_ENCRYPTION_KEY=NEW_KEY,
        PHI_ENCRYPTION_KEY_PREVIOUS=PUBLISHED_DEV_KEY,
    )
    assert result.returncode != 0
    assert "PHI_ENCRYPTION_KEY_PREVIOUS[0]" in result.stderr


def test_production_refuses_to_start_with_no_phi_key_at_all(tmp_path):
    result = _boot(tmp_path, **PRODUCTION_ENV)
    assert result.returncode != 0
    assert "PHI_ENCRYPTION_KEY is unset" in result.stderr


def test_production_refuses_the_default_jwt_secret(tmp_path):
    env = {**PRODUCTION_ENV, "JWT_SECRET": "change-me-in-every-environment"}
    result = _boot(tmp_path, **env, PHI_ENCRYPTION_KEY=NEW_KEY)
    assert result.returncode != 0
    assert "JWT_SECRET" in result.stderr


def test_a_malformed_key_is_rejected_at_boot_not_at_the_first_patient(tmp_path):
    """Without this the process starts, passes its health check, takes
    traffic, and throws on the first PHI write.
    """
    result = _boot(tmp_path, ENVIRONMENT="development", PHI_ENCRYPTION_KEY="not-a-fernet-key")
    assert result.returncode != 0
    assert "not a valid Fernet key" in result.stderr


def test_the_same_key_listed_as_both_current_and_previous_is_rejected(tmp_path):
    """Configured that way, a rotation run would report success while
    rewriting every row back to the key it was already under."""
    result = _boot(
        tmp_path,
        ENVIRONMENT="development",
        PHI_ENCRYPTION_KEY=NEW_KEY,
        PHI_ENCRYPTION_KEY_PREVIOUS=NEW_KEY,
    )
    assert result.returncode != 0
    assert "also appears in PHI_ENCRYPTION_KEY_PREVIOUS" in result.stderr


def test_a_correctly_configured_production_process_does_start(tmp_path):
    """The guard has to be passable, or it is just an outage."""
    result = _boot(tmp_path, **PRODUCTION_ENV, PHI_ENCRYPTION_KEY=NEW_KEY)
    assert result.returncode == 0, result.stderr
