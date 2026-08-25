"""Import every model module here so Base.metadata is complete for Alembic
autogenerate and for create_all() in tests.
"""

from app.models.audit_log import AuditLog
from app.models.clinician import Clinician
from app.models.consent import ConsentLedgerEntry
from app.models.encounter import Encounter
from app.models.login_attempt import LoginAttempt
from app.models.note import Note, NoteRevision, NoteStatus
from app.models.patient import Patient
from app.models.refresh_token import RefreshToken
from app.models.transcript import Transcript

__all__ = [
    "AuditLog",
    "Clinician",
    "ConsentLedgerEntry",
    "Encounter",
    "LoginAttempt",
    "Note",
    "NoteRevision",
    "NoteStatus",
    "Patient",
    "RefreshToken",
    "Transcript",
]
