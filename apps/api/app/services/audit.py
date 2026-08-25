import json

from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog


def record(
    db: Session,
    *,
    actor_clinician_id: str | None,
    action: str,
    entity_type: str,
    entity_id: str,
    diff: dict | None = None,
) -> None:
    """The single write path for app_logs (P0-8). Called explicitly from
    routes that read/change sensitive entities (patients, notes, consent) —
    explicit call sites, rather than a generic request-logging middleware,
    so `action`/`entity_type` stay meaningful (e.g. "note.sign") instead of
    a raw HTTP method + path.
    """
    db.add(
        AuditLog(
            actor_clinician_id=actor_clinician_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            diff=json.dumps(diff) if diff is not None else None,
        )
    )
    db.commit()
