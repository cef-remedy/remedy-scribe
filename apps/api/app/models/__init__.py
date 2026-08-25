"""Import every model module here so Base.metadata is complete for Alembic
autogenerate and for create_all() in tests.
"""

from app.models.audit_log import AuditLog
from app.models.clinician import Clinician
from app.models.consent import ConsentLedgerEntry
from app.models.encounter import Encounter
from app.models.note import Note, NoteRevision, NoteStatus
from app.models.patient import Patient

__all__ = [
    "AuditLog",
    "Clinician",
    "ConsentLedgerEntry",
    "Encounter",
    "Note",
    "NoteRevision",
    "NoteStatus",
    "Patient",
]
